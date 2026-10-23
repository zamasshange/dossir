from flask import (
    Flask,
    render_template,
    request,
    send_from_directory,
    Response,
    flash,
    redirect,
    url_for,
)
from werkzeug.exceptions import NotFound
import logging
import os
import asyncio
import json
import queue
import uuid
from datetime import datetime
from threading import Thread
from typing import Any, Dict
import maigret
import maigret.settings
from maigret.checking import build_cloudflare_bypass_config
from maigret.result import MaigretCheckResult, MaigretCheckStatus
from maigret.sites import MaigretDatabase
from maigret.report import generate_report_context
from maigret.web.names import (
    is_person_name,
    lookup_public_name,
    name_handles,
    split_queries,
)

app = Flask(__name__)
# Use environment variable for secret key, generate random one if not set
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24).hex())

# add background job tracking
background_jobs: Dict[str, Any] = {}
job_results = {}

# Live (streaming) scan jobs, keyed by job_id. Each entry:
#   {'queue': Queue, 'cancelled': bool, 'loop': event loop, 'task': asyncio task}
# ponytail: in-memory, single-process. Entries are dropped when their SSE stream
# ends; a stream that's never consumed lingers — fine for a local tool.
live_jobs: Dict[str, Any] = {}


class StreamNotify:
    """query_notify shim: pushes each per-site check into a queue as an SSE event.

    maigret's search loop calls update() once per finished site check, which is
    exactly the granularity we want to stream to the browser.
    """

    def __init__(self, event_queue, username, display_name=None, emit_roster=True):
        self.q = event_queue
        self.username = username
        self.display_name = display_name or username
        self.emit_roster = emit_roster
        self.total = 0
        self.checked = 0
        self.sites = {}
        # Per-site results collected so far, in the shape build_reports()
        # expects. If the scan gets cancelled mid-way (Stop button), this is
        # what's left to report on — otherwise every already-streamed
        # 'found' event is silently discarded because the search() task
        # never returns to hand back its own results dict.
        self.results = {}

    def set_total(self, total):
        self.total = total
        self.q.put(
            {
                'type': 'start',
                'username': self.display_name,
                'handle': self.username,
                'total': total,
            }
        )

    def set_sites(self, sites):
        self.sites = sites
        # A short roster lets the page draw every site as free or taken.
        # Wider lookups stay on the profile list; an 8,000-name dump is noise.
        # Name searches skip it: several spellings of one name would redraw
        # the same board over and over.
        if self.emit_roster and sites and len(sites) <= 120:
            roster = [
                {'name': name, 'url': getattr(site, 'url_main', '') or ''}
                for name, site in sites.items()
            ]
            self.q.put({'type': 'roster', 'username': self.username, 'sites': roster})

    def update(self, result, is_similar=False):
        self.checked += 1
        if not is_similar:
            entry = {'status': result, 'url_user': result.site_url_user}
            site = self.sites.get(result.site_name)
            if site is not None:
                entry['site'] = site
                entry['url_main'] = site.url_main
            self.results[result.site_name] = entry
        if result.status == MaigretCheckStatus.CLAIMED and not is_similar:
            ids = {
                k: v
                for k, v in (result.ids_data or {}).items()
                if k != '_extractor' and isinstance(v, (str, int, float))
            }
            site = self.sites.get(result.site_name)
            rank = getattr(site, 'alexa_rank', None) if site is not None else None
            if rank is not None and rank >= 10**12:
                rank = None
            self.q.put(
                {
                    'type': 'found',
                    'username': self.display_name,
                    'handle': result.username or self.username,
                    'site': result.site_name,
                    'url': result.site_url_user,
                    'ids': ids,
                    'rank': rank,
                    'priority': known_priority(result.site_name),
                }
            )
        progress = {
            'type': 'progress',
            'checked': self.checked,
            'total': self.total,
            'site': result.site_name,
        }
        if not is_similar:
            progress['status'] = {
                MaigretCheckStatus.CLAIMED: 'claimed',
                MaigretCheckStatus.AVAILABLE: 'available',
                MaigretCheckStatus.UNKNOWN: 'unknown',
                MaigretCheckStatus.ILLEGAL: 'illegal',
            }.get(result.status, 'unknown')
            progress['username'] = result.username or self.username
        self.q.put(progress)

    # No-op sinks for the rest of the notifier surface the search loop touches.
    def start(self, message=None, id_type="username"):
        pass

    def finish(self, message=None):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def success(self, *a, **k):
        pass

    def enrich(self, *a, **k):
        pass


# Configuration
app.config["MAIGRET_DB_FILE"] = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'resources', 'data.json'
)
app.config["COOKIES_FILE"] = "cookies.txt"
app.config["UPLOAD_FOLDER"] = 'uploads'
app.config["REPORTS_FOLDER"] = os.path.abspath('/tmp/maigret_reports')
app.config["SETTINGS_FILE"] = "web_settings.json"

# Search-wide defaults, editable from the Settings modal (base.html). Persisted
# to app.config["SETTINGS_FILE"] so they survive a process restart.
DEFAULT_SETTINGS = {
    'timeout': 10,
    'top_sites': 500,
    'tags': [],
    'excluded_tags': [],
    'site_list': [],
    'proxy': '',
    'tor_proxy': '',
    'i2p_proxy': '',
    'permute': False,
    'disable_recursive_search': False,
    'disable_extracting': False,
    'with_domains': False,
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    path = app.config["SETTINGS_FILE"]
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                settings.update(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logging.error(f"Failed to load settings from {path}: {e}")
    return settings


def save_settings(settings):
    with open(app.config["SETTINGS_FILE"], 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)


def parse_settings_form(form):
    try:
        timeout = int(form.get('timeout'))
    except (TypeError, ValueError):
        timeout = DEFAULT_SETTINGS['timeout']

    try:
        top_sites = int(form.get('top_sites'))
    except (TypeError, ValueError):
        top_sites = DEFAULT_SETTINGS['top_sites']

    return {
        'timeout': timeout,
        'top_sites': top_sites,
        'tags': form.getlist('tags'),
        'excluded_tags': form.getlist('excluded_tags'),
        'site_list': [s.strip() for s in form.get('site', '').split(',') if s.strip()],
        'proxy': form.get('proxy', '').strip(),
        'tor_proxy': form.get('tor_proxy', '').strip(),
        'i2p_proxy': form.get('i2p_proxy', '').strip(),
        'permute': 'permute' in form,
        'disable_recursive_search': 'disable_recursive_search' in form,
        'disable_extracting': 'disable_extracting' in form,
        'with_domains': 'with_domains' in form,
    }


@app.context_processor
def inject_settings():
    return {'web_settings': load_settings()}


def setup_logger(log_level, name):
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    return logger


async def maigret_search(username, options, query_notify=None):
    logger = setup_logger(logging.WARNING, 'maigret')
    try:
        settings = maigret.settings.Settings()
        settings.load()
        cf_bypass_config = build_cloudflare_bypass_config(settings)
        if cf_bypass_config:
            modules_summary = ", ".join(
                f"{m.get('name', m.get('method'))}({m.get('url')})"
                for m in cf_bypass_config["modules"]
            )
            logger.info(
                f"Cloudflare webgate active: triggers={cf_bypass_config['trigger_protection']}, "
                f"modules=[{modules_summary}]"
            )

        db = MaigretDatabase().load_from_path(app.config["MAIGRET_DB_FILE"])

        limit = int(options.get('top_sites') or 500)
        # Known-first presets need the wide list first, then a cut. A plain
        # top-N slice would drop familiar sites that sit just outside that rank.
        fetch_top = 999999999 if options.get('all_sites') or options.get('prefer_known') else limit

        tags = options.get('tags', [])
        excluded_tags = options.get('excluded_tags', [])
        site_list = options.get('site_list', [])
        logger.info(f"Filtering sites by tags: {tags}, excluded: {excluded_tags}")

        sites = db.ranked_sites_dict(
            top=fetch_top,
            tags=tags,
            excluded_tags=excluded_tags,
            names=site_list,
            disabled=False,
            id_type='username',
        )
        if options.get('prefer_known') and not options.get('all_sites'):
            sites = order_known_first(sites, limit)

        logger.info(f"Found {len(sites)} sites matching the tag criteria")

        if query_notify is not None and hasattr(query_notify, 'set_total'):
            query_notify.set_total(len(sites))
        if query_notify is not None and hasattr(query_notify, 'set_sites'):
            query_notify.set_sites(sites)

        results = await maigret.search(
            username=username,
            site_dict=sites,
            timeout=int(options.get('timeout', 30)),
            logger=logger,
            id_type='username',
            query_notify=query_notify,
            no_progressbar=bool(query_notify),
            cookies=app.config["COOKIES_FILE"] if options.get('use_cookies') else None,
            is_parsing_enabled=(not options.get('disable_extracting', False)),
            recursive_search_enabled=(
                not options.get('disable_recursive_search', False)
            ),
            check_domains=options.get('with_domains', False),
            proxy=options.get('proxy', None),
            tor_proxy=options.get('tor_proxy', None),
            i2p_proxy=options.get('i2p_proxy', None),
            cloudflare_bypass=cf_bypass_config,
        )
        return results
    except Exception as e:
        logger.error(f"Error during search: {str(e)}")
        raise


def variant_options(options):
    """Extra spellings of a name stay on the famous sites.

    The closest handle still uses whatever scope the user picked. Repeating
    an all-sites scan for every spelling would multiply the work by eight.
    """
    if options.get('all_sites') or int(options.get('top_sites') or 0) > 100:
        narrowed = dict(options)
        narrowed['all_sites'] = False
        narrowed['prefer_known'] = True
        narrowed['top_sites'] = 50
        return narrowed
    return options


def _merge_results(merged, results, handle):
    for site_name, entry in (results or {}).items():
        key = site_name if site_name not in merged else f'{site_name} ({handle})'
        merged[key] = entry


def _name_page_result(display_name, page):
    ids = {}
    if page.get('about'):
        ids['about'] = page['about']
    if page.get('handle'):
        ids['handle'] = page['handle']
    status = MaigretCheckResult(
        username=display_name,
        site_name=page['site'],
        site_url_user=page['url'],
        status=MaigretCheckStatus.CLAIMED,
        ids_data=ids or None,
        tags=[],
    )
    return {'status': status, 'url_user': page['url']}


def _emit_name_page(queue, display_name, page):
    if queue is None:
        return
    ids = {}
    if page.get('about'):
        ids['about'] = page['about']
    if page.get('handle'):
        ids['handle'] = page['handle']
    priority = known_priority(page['site'])
    if priority is None and page['site'] == 'Website':
        priority = 40
    queue.put(
        {
            'type': 'found',
            'username': display_name,
            'handle': page.get('handle') or '',
            'site': page['site'],
            'url': page['url'],
            'ids': ids,
            'rank': None,
            'priority': priority,
            'via': 'name',
        }
    )


async def _run_query(job, query, options):
    """Search one field value. A real name stays one subject.

    Returns (stopped, display_name, results_dict).
    """
    queue = job['queue'] if job is not None else None
    query = (query or '').strip()
    if not query:
        return False, query, {}

    if not is_person_name(query):
        notify = StreamNotify(queue, query) if queue is not None else None
        try:
            if job is not None:
                task = asyncio.ensure_future(
                    maigret_search(query, options, query_notify=notify)
                )
                job['task'] = task
                results = await task
            else:
                results = await maigret_search(query, options)
            return False, query, results or {}
        except asyncio.CancelledError:
            if queue is not None:
                queue.put({'type': 'stopped', 'username': query})
            return True, query, (notify.results if notify else {})
        except Exception as exc:
            logging.error(f"Error searching username {query}: {exc}")
            if queue is not None:
                queue.put({'type': 'error', 'message': str(exc), 'username': query})
            return False, query, (notify.results if notify and notify.results else {})

    handles = name_handles(query)
    wide = bool(options.get('all_sites') or int(options.get('top_sites') or 0) > 100)
    if queue is not None:
        queue.put(
            {
                'type': 'name',
                'query': query,
                'handles': handles,
                'about': '',
                'wide': wide,
            }
        )

    merged = {}
    try:
        pages = await asyncio.to_thread(lookup_public_name, query)
    except asyncio.CancelledError:
        return True, query, merged
    except Exception as exc:
        logging.info('Name lookup failed for %s: %s', query, exc)
        pages = []

    about = ''
    for page in pages:
        about = about or page.get('about') or ''
        _merge_results(merged, {page['site']: _name_page_result(query, page)}, query)
        _emit_name_page(queue, query, page)
    if queue is not None and (about or pages):
        queue.put(
            {
                'type': 'name',
                'query': query,
                'handles': handles,
                'about': about,
                'wide': wide,
            }
        )

    stopped = False
    for index, handle in enumerate(handles):
        if job is not None and job.get('cancelled'):
            stopped = True
            break
        opts = options if index == 0 else variant_options(options)
        notify = (
            StreamNotify(queue, handle, display_name=query, emit_roster=False)
            if queue is not None
            else None
        )
        try:
            if job is not None:
                task = asyncio.ensure_future(
                    maigret_search(handle, opts, query_notify=notify)
                )
                job['task'] = task
                found = await task
            else:
                found = await maigret_search(handle, opts)
        except asyncio.CancelledError:
            found = notify.results if notify else {}
            stopped = True
        except Exception as exc:
            logging.error(f"Error searching handle {handle} for {query}: {exc}")
            if queue is not None:
                queue.put({'type': 'error', 'message': str(exc), 'username': query})
            found = notify.results if notify and notify.results else {}
        _merge_results(merged, found, handle)
        if stopped:
            break
    if stopped and queue is not None:
        queue.put({'type': 'stopped', 'username': query})
    return stopped, query, merged


async def search_multiple_usernames(usernames, options):
    results = []
    for username in usernames:
        try:
            _stopped, query, found = await _run_query(None, username, options)
        except Exception as e:
            logging.error(f"Error searching username {username}: {str(e)}")
            continue
        if found:
            results.append((query, 'username', found))
    return results


def sanitize_username_for_path(username: str) -> str:
    """Remove path separators and dangerous components from username for safe file path usage."""
    # Replace path separators and null bytes
    sanitized = username.replace('/', '_').replace('\\', '_').replace('\0', '_')
    # Remove . and .. components
    sanitized = sanitized.strip('.')
    # If empty after sanitization, use a fallback
    return sanitized or '_'


def build_reports(general_results, usernames, session_key):
    """Write per-username CSV/JSON/PDF/HTML reports + combined graph to disk.

    Shared by the background /search job and the live SSE /api/scan job, so
    both flows land on the same results.html (report buttons + profile list).
    """
    os.makedirs(app.config["REPORTS_FOLDER"], exist_ok=True)
    session_folder = os.path.join(
        app.config["REPORTS_FOLDER"], f"search_{session_key}"
    )
    os.makedirs(session_folder, exist_ok=True)

    graph_path = os.path.join(session_folder, "combined_graph.html")
    maigret.report.save_graph_report(
        graph_path,
        general_results,
        MaigretDatabase().load_from_path(app.config["MAIGRET_DB_FILE"]),
    )

    individual_reports = []
    found_count = 0
    for username, id_type, results in general_results:
        safe_username = sanitize_username_for_path(username)
        report_base = os.path.join(session_folder, f"report_{safe_username}")

        csv_path = f"{report_base}.csv"
        json_path = f"{report_base}.json"
        pdf_path = f"{report_base}.pdf"
        html_path = f"{report_base}.html"

        context = generate_report_context(general_results)

        maigret.report.save_csv_report(csv_path, username, results)
        maigret.report.save_json_report(
            json_path, username, results, report_type='ndjson'
        )
        maigret.report.save_pdf_report(pdf_path, context)
        maigret.report.save_html_report(html_path, context)

        claimed_profiles = []
        for site_name, site_data in results.items():
            if (
                site_data.get('status')
                and site_data['status'].status == MaigretCheckStatus.CLAIMED
            ):
                claimed_profiles.append(
                    {
                        'site_name': site_name,
                        'url': site_data.get('url_user', ''),
                        'tags': (
                            site_data.get('status').tags
                            if site_data.get('status')
                            else []
                        ),
                    }
                )

        found_count += len(claimed_profiles)
        individual_reports.append(
            {
                'username': username,
                'csv_file': os.path.join(
                    f"search_{session_key}", f"report_{safe_username}.csv"
                ),
                'json_file': os.path.join(
                    f"search_{session_key}", f"report_{safe_username}.json"
                ),
                'pdf_file': os.path.join(
                    f"search_{session_key}", f"report_{safe_username}.pdf"
                ),
                'html_file': os.path.join(
                    f"search_{session_key}", f"report_{safe_username}.html"
                ),
                'claimed_profiles': claimed_profiles,
            }
        )

    return {
        'status': 'completed',
        'session_folder': f"search_{session_key}",
        'graph_file': os.path.join(f"search_{session_key}", "combined_graph.html"),
        'usernames': usernames,
        'individual_reports': individual_reports,
        'found_count': found_count,
    }


def process_search_task(usernames, options, timestamp):
    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        general_results = loop.run_until_complete(
            search_multiple_usernames(usernames, options)
        )
        job_results[timestamp] = build_reports(general_results, usernames, timestamp)

    except Exception as e:
        logging.error(f"Error in search task for timestamp {timestamp}: {str(e)}")
        job_results[timestamp] = {
            'status': 'failed',
            'error': str(e),
            'usernames': usernames,
        }
    finally:
        job_results[timestamp]['started_at'] = started_at
        background_jobs[timestamp]['completed'] = True


def parse_usernames(form):
    return split_queries(form.get('usernames', ''))


# Household platforms, most familiar first. Top 10 / 50 / 100 start here,
# then fill any remaining slots from the usual popularity ranking.
WELL_KNOWN = (
    'Facebook',
    'Instagram',
    'TikTok',
    'YouTube',
    'Twitter',
    'Reddit',
    'Snapchat',
    'LinkedIn',
    'Pinterest',
    'GitHub',
    'Discord',
    'Telegram',
    'Twitch',
    'Spotify',
    'WhatsApp',
    'Threads',
    'Tumblr',
    'Flickr',
    'Medium',
    'VK',
    'Steam',
    'Quora',
    'Wikipedia',
    'Vimeo',
    'Roblox',
    'Bluesky',
    'Patreon',
    'SoundCloud',
    'Behance',
    'GitLab',
    'StackOverflow',
    'IMDb',
    'Goodreads',
    'Strava',
    'DeviantArt',
    'Xbox',
    'PlayStation',
    'EpicGames',
    'PayPal',
    'Amazon',
    'eBay',
    'Etsy',
    'Netflix',
    'Letterboxd',
    'Duolingo',
    'Mastodon',
    'Kick',
    'OnlyFans',
    'Binance',
    'Chess.com',
    'ArtStation',
    'Dribbble',
)
WELL_KNOWN_INDEX = {name.lower(): index for index, name in enumerate(WELL_KNOWN)}
KNOWN_PRESETS = {'10': 10, '50': 50, '100': 100}


def known_priority(site_name):
    if not site_name:
        return None
    return WELL_KNOWN_INDEX.get(site_name.lower())


def order_known_first(sites, limit):
    """Famous platforms first, then the rest of `sites` in its current rank order."""
    by_name = {name.lower(): (name, site) for name, site in sites.items()}
    ordered = []
    seen = set()
    for known in WELL_KNOWN:
        key = known.lower()
        if key in by_name and key not in seen:
            ordered.append(by_name[key])
            seen.add(key)
        if len(ordered) >= limit:
            break
    if len(ordered) < limit:
        for name, site in sites.items():
            key = name.lower()
            if key in seen:
                continue
            ordered.append((name, site))
            seen.add(key)
            if len(ordered) >= limit:
                break
    return dict(ordered)


def parse_search_options(form):
    settings = load_settings()
    # Platforms chosen on the search form limit this lookup. An empty field
    # keeps whatever was saved in Settings.
    posted_sites = [
        s.strip() for s in (form.get('site') or '').split(',') if s.strip()
    ]
    mode = form.get('mode') or 'fast'
    if mode == 'full':
        all_sites = True
        top_sites = settings['top_sites']
        prefer_known = False
    elif mode in KNOWN_PRESETS:
        all_sites = False
        top_sites = KNOWN_PRESETS[mode]
        prefer_known = True
    else:
        all_sites = False
        top_sites = settings['top_sites']
        prefer_known = False
    return {
        'top_sites': top_sites,
        'timeout': settings['timeout'],
        'use_cookies': 'use_cookies' in form,
        'all_sites': all_sites,
        'prefer_known': prefer_known,
        'disable_recursive_search': settings['disable_recursive_search'],
        'disable_extracting': settings['disable_extracting'],
        'with_domains': settings['with_domains'],
        'proxy': settings['proxy'] or None,
        'tor_proxy': settings['tor_proxy'] or None,
        'i2p_proxy': settings['i2p_proxy'] or None,
        'permute': settings['permute'],
        'tags': settings['tags'],
        'excluded_tags': settings['excluded_tags'],
        'site_list': posted_sites or settings['site_list'],
    }


async def _stream_search(job, usernames, options):
    general_results = []
    for username in usernames:
        if job['cancelled']:
            break
        stopped, query, results = await _run_query(job, username, options)
        if results:
            general_results.append((query, 'username', results))
        if stopped:
            break
    return general_results


def run_stream_job(job_id, usernames, options):
    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    job = live_jobs[job_id]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    job['loop'] = loop
    general_results = []
    try:
        general_results = loop.run_until_complete(
            _stream_search(job, usernames, options)
        )
    except Exception as e:
        job['queue'].put({'type': 'error', 'message': str(e)})
    finally:
        loop.close()

    # Same report files + results page as the classic /search flow, so the
    # live graph is a progress view, not a replacement for the report.
    done_event = {'type': 'done'}
    if general_results:
        try:
            job_results[job_id] = build_reports(general_results, usernames, job_id)
            job_results[job_id]['started_at'] = started_at
            job_results[job_id]['purpose'] = job.get('purpose', 'lookup')
            done_event['redirect'] = f"/results/search_{job_id}"
        except Exception as e:
            logging.error(f"Error building reports for live scan {job_id}: {str(e)}")
    job['queue'].put(done_event)


PURPOSES = ('free', 'mine', 'lookup', 'copy')


def start_live_job(usernames, options, purpose='lookup'):
    if purpose not in PURPOSES:
        purpose = 'lookup'
    job_id = uuid.uuid4().hex
    live_jobs[job_id] = {
        'queue': queue.Queue(),
        'cancelled': False,
        'loop': None,
        'task': None,
        'purpose': purpose,
    }
    Thread(target=run_stream_job, args=(job_id, usernames, options)).start()
    return job_id


@app.route('/api/scan', methods=['POST'])
def scan_start():
    usernames = parse_usernames(request.form)
    if not usernames:
        return {'error': 'At least one username is required'}, 400

    options = parse_search_options(request.form)
    job_id = start_live_job(usernames, options)
    return {'job_id': job_id}


@app.route('/api/scan/<job_id>/stream')
def scan_stream(job_id):
    job = live_jobs.get(job_id)
    if not job:
        return "Unknown job", 404

    def gen():
        try:
            while True:
                event = job['queue'].get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get('type') == 'done':
                    break
        finally:
            live_jobs.pop(job_id, None)

    return Response(gen(), mimetype='text/event-stream')


@app.route('/api/scan/<job_id>/stop', methods=['POST'])
def scan_stop(job_id):
    job = live_jobs.get(job_id)
    if not job:
        return {'error': 'unknown job'}, 404

    job['cancelled'] = True
    loop = job.get('loop')
    task = job.get('task')
    if loop and task:
        loop.call_soon_threadsafe(task.cancel)
    return {'ok': True}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/sites')
def api_sites():
    """Site names/URLs for the Filters site-picker datalist, fetched lazily
    from the Settings modal instead of loading the DB on every page render."""
    db = MaigretDatabase().load_from_path(app.config["MAIGRET_DB_FILE"])
    site_options = []
    platforms = []
    for site in db.sites:
        site_options.append(site.name)
        if site.url_main and site.url_main not in site_options:
            site_options.append(site.url_main)
        platforms.append({'name': site.name, 'url': site.url_main or ''})
    platforms.sort(key=lambda item: item['name'].lower())
    return {'sites': sorted(set(site_options)), 'platforms': platforms}


@app.route('/settings', methods=['POST'])
def settings_update():
    save_settings(parse_settings_form(request.form))
    flash('Settings saved.', 'success')
    return redirect(request.referrer or url_for('index'))


@app.route('/history')
def history():
    entries = sorted(
        job_results.values(), key=lambda r: r.get('started_at', ''), reverse=True
    )
    return render_template('history.html', entries=entries)


@app.route('/live', methods=['POST'])
def live_start():
    usernames = parse_usernames(request.form)
    if not usernames:
        flash('At least one username is required', 'danger')
        return redirect(url_for('index'))

    options = parse_search_options(request.form)
    purpose = request.form.get('purpose') or 'lookup'
    job_id = start_live_job(usernames, options, purpose)
    return redirect(url_for('live_results', job_id=job_id))


@app.route('/live/<job_id>')
def live_results(job_id):
    result = job_results.get(job_id)
    if job_id not in live_jobs and not result:
        flash('Unknown or expired scan session.', 'danger')
        return redirect(url_for('index'))

    done_redirect = None
    if result and result.get('status') == 'completed':
        done_redirect = url_for('results', session_id=result['session_folder'])

    purpose = 'lookup'
    job = live_jobs.get(job_id)
    if job and job.get('purpose') in PURPOSES:
        purpose = job['purpose']
    elif result and result.get('purpose') in PURPOSES:
        purpose = result['purpose']

    return render_template(
        'live.html', job_id=job_id, done_redirect=done_redirect, purpose=purpose
    )


# Modified search route
@app.route('/search', methods=['POST'])
def search():
    usernames = parse_usernames(request.form)
    if not usernames:
        flash('At least one username is required', 'danger')
        return redirect(url_for('index'))

    # Create timestamp for this search session
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    options = parse_search_options(request.form)
    logging.info(
        f"Starting search for usernames: {usernames} with tags: {options['tags']}, "
        f"excluded: {options['excluded_tags']}"
    )

    # Start background job
    background_jobs[timestamp] = {
        'completed': False,
        'thread': Thread(
            target=process_search_task, args=(usernames, options, timestamp)
        ),
    }
    background_jobs[timestamp]['thread'].start()  # type: ignore[union-attr]

    return redirect(url_for('status', timestamp=timestamp))


@app.route('/status/<timestamp>')
def status(timestamp):
    logging.info(f"Status check for timestamp: {timestamp}")

    # Validate timestamp
    if timestamp not in background_jobs:
        flash('Invalid search session.', 'danger')
        logging.error(f"Invalid search session: {timestamp}")
        return redirect(url_for('index'))

    # Check if job is completed
    if background_jobs[timestamp]['completed']:
        result = job_results.get(timestamp)
        if not result:
            flash('No results found for this search session.', 'warning')
            logging.error(f"No results found for completed session: {timestamp}")
            return redirect(url_for('index'))

        if result['status'] == 'completed':
            # Note: use the session_folder from the results to redirect
            return redirect(url_for('results', session_id=result['session_folder']))
        else:
            error_msg = result.get('error', 'Unknown error occurred.')
            flash(f'Search failed: {error_msg}', 'danger')
            logging.error(f"Search failed for session {timestamp}: {error_msg}")
            return redirect(url_for('index'))

    # If job is still running, show a status page
    return render_template('status.html', timestamp=timestamp)


@app.route('/results/<session_id>')
def results(session_id):
    # Find completed results that match this session_folder
    result_data = next(
        (
            r
            for r in job_results.values()
            if r.get('status') == 'completed' and r['session_folder'] == session_id
        ),
        None,
    )

    if not result_data:
        flash('No results found for this session ID.', 'danger')
        logging.error(f"Results for session {session_id} not found in job_results.")
        return redirect(url_for('index'))

    return render_template(
        'results.html',
        usernames=result_data['usernames'],
        graph_file=result_data['graph_file'],
        individual_reports=result_data['individual_reports'],
        timestamp=session_id.replace('search_', ''),
        purpose=result_data.get('purpose') or 'lookup',
    )


@app.route('/reports/<path:filename>')
def download_report(filename):
    reports_root = app.config["REPORTS_FOLDER"]
    os.makedirs(reports_root, exist_ok=True)
    try:
        return send_from_directory(reports_root, filename)
    except NotFound:
        return "File not found", 404
    except Exception as e:
        logging.error(f"Error serving file {filename}: {str(e)}")
        return "File not found", 404


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']

    # Host configuration: secure by default
    # Use 127.0.0.1 for local development, 0.0.0.0 only if explicitly set
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    port = int(os.getenv('FLASK_PORT', '5000'))

    app.run(host=host, port=port, debug=debug_mode)
