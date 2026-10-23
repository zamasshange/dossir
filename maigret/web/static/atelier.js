(function () {
    const root = document.documentElement;
    const themeBtn = document.getElementById('theme-toggle');
    const modal = document.getElementById('settingsModal');
    const openBtn = document.getElementById('settings-toggle');
    const panel = modal ? modal.querySelector('.drawer-panel') : null;
    const bootNode = document.getElementById('settings-boot');
    const boot = bootNode ? JSON.parse(bootNode.textContent) : {};

    function currentTheme() {
        return root.getAttribute('data-theme') === 'paper' ? 'paper' : 'ink';
    }

    function paintTheme(theme) {
        root.setAttribute('data-theme', theme);
        try {
            localStorage.setItem('maigret-theme', theme);
        } catch (e) {
            /* private mode */
        }
        if (themeBtn) {
            const paper = theme === 'paper';
            themeBtn.textContent = paper ? 'Ink' : 'Paper';
            themeBtn.title = paper ? 'Switch to the ink desk' : 'Switch to the paper desk';
            themeBtn.setAttribute('aria-pressed', paper ? 'true' : 'false');
        }
        document.dispatchEvent(new CustomEvent('maigret-theme'));
    }

    if (themeBtn) {
        themeBtn.addEventListener('click', function () {
            paintTheme(currentTheme() === 'paper' ? 'ink' : 'paper');
        });
        paintTheme(currentTheme());
    }

    function focusable() {
        if (!panel) return [];
        return Array.from(
            panel.querySelectorAll('a, button, input, select, textarea, [tabindex]')
        ).filter(function (el) {
            return !el.disabled && el.offsetParent !== null && el.tabIndex !== -1;
        });
    }

    function openSettings() {
        if (!modal) return;
        modal.classList.add('is-open');
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('drawer-lock');
        if (openBtn) openBtn.setAttribute('aria-expanded', 'true');
        loadSites();
        const first = focusable()[0];
        if (first) first.focus();
    }

    function closeSettings() {
        if (!modal) return;
        modal.classList.remove('is-open');
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('drawer-lock');
        if (openBtn) {
            openBtn.setAttribute('aria-expanded', 'false');
            openBtn.focus();
        }
    }

    if (openBtn) openBtn.addEventListener('click', openSettings);
    if (location.hash === '#settings') openSettings();

    document.querySelectorAll('[data-open-settings]').forEach(function (el) {
        el.addEventListener('click', function (e) {
            e.preventDefault();
            openSettings();
        });
    });

    if (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target.closest('[data-close]')) closeSettings();
        });
        document.addEventListener('keydown', function (e) {
            if (!modal.classList.contains('is-open')) return;
            if (e.key === 'Escape') {
                e.preventDefault();
                closeSettings();
                return;
            }
            if (e.key !== 'Tab') return;
            const items = focusable();
            if (!items.length) return;
            const first = items[0];
            const last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        });
    }

    const tagCloud = document.getElementById('settingsTagCloud');
    const hiddenSelect = document.getElementById('settingsTags');
    const excludedSelect = document.getElementById('settingsExcludedTags');

    if (tagCloud && hiddenSelect && excludedSelect) {
        const initialSelected = boot.tags || [];
        const initialExcluded = boot.excluded || [];
        const allTags = Array.from(hiddenSelect.options).map(function (opt) {
            return {
                value: opt.value,
                label: opt.text,
                group: opt.dataset.group || 'category',
            };
        });

        function updateTagSelects() {
            Array.from(hiddenSelect.options).forEach(function (opt) {
                opt.selected = false;
            });
            excludedSelect.innerHTML = '';
            tagCloud.querySelectorAll('.tag').forEach(function (tagEl) {
                const val = tagEl.dataset.value;
                if (tagEl.classList.contains('selected')) {
                    const option = Array.from(hiddenSelect.options).find(function (opt) {
                        return opt.value === val;
                    });
                    if (option) option.selected = true;
                } else if (tagEl.classList.contains('excluded')) {
                    const opt = document.createElement('option');
                    opt.value = val;
                    opt.selected = true;
                    excludedSelect.appendChild(opt);
                }
            });
        }

        let lastGroup = '';
        allTags.forEach(function (tag) {
            if (tag.group !== lastGroup && tag.group === 'country') {
                const separator = document.createElement('p');
                separator.className = 'tag-break';
                separator.textContent = 'Countries';
                tagCloud.appendChild(separator);
            }
            lastGroup = tag.group;

            const tagElement = document.createElement('button');
            tagElement.type = 'button';
            tagElement.className = 'tag';
            tagElement.textContent = tag.label;
            tagElement.dataset.value = tag.value;
            if (initialSelected.indexOf(tag.value) !== -1) tagElement.classList.add('selected');
            if (initialExcluded.indexOf(tag.value) !== -1) tagElement.classList.add('excluded');
            tagElement.addEventListener('click', function () {
                if (this.classList.contains('selected')) {
                    this.classList.remove('selected');
                    this.classList.add('excluded');
                } else if (this.classList.contains('excluded')) {
                    this.classList.remove('excluded');
                } else {
                    this.classList.add('selected');
                }
                updateTagSelects();
            });
            tagCloud.appendChild(tagElement);
        });
        updateTagSelects();

        const tagFilter = document.getElementById('tagFilter');
        if (tagFilter) {
            tagFilter.addEventListener('input', function () {
                const q = this.value.trim().toLowerCase();
                tagCloud.querySelectorAll('.tag').forEach(function (el) {
                    el.hidden = Boolean(q) && el.textContent.toLowerCase().indexOf(q) === -1;
                });
            });
        }

        const clearTags = document.getElementById('clearTags');
        if (clearTags) {
            clearTags.addEventListener('click', function () {
                tagCloud.querySelectorAll('.tag').forEach(function (el) {
                    el.classList.remove('selected', 'excluded');
                });
                updateTagSelects();
            });
        }
    }

    const siteInput = document.getElementById('settingsSiteInput');
    const siteHiddenInput = document.getElementById('settings-site');
    const selectedSitesContainer = document.getElementById('settingsSelectedSites');
    const siteDatalist = document.getElementById('settingsSiteOptions');
    let selectedSites = new Set(boot.sites || []);
    let sitesLoaded = false;

    function updateSiteHiddenInput() {
        if (siteHiddenInput) siteHiddenInput.value = Array.from(selectedSites).join(',');
    }

    function renderSites() {
        if (!selectedSitesContainer) return;
        selectedSitesContainer.replaceChildren();
        selectedSites.forEach(function (site) {
            const chip = document.createElement('span');
            chip.className = 'chip';
            const label = document.createElement('span');
            label.textContent = site;
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'chip-x';
            remove.dataset.site = site;
            remove.setAttribute('aria-label', 'Remove ' + site);
            remove.textContent = '×';
            chip.append(label, remove);
            selectedSitesContainer.appendChild(chip);
        });
    }

    function addSite(site) {
        if (site && !selectedSites.has(site)) {
            selectedSites.add(site);
            updateSiteHiddenInput();
            renderSites();
        }
    }

    renderSites();

    function loadSites() {
        if (sitesLoaded || !siteDatalist || !boot.sitesUrl) return;
        sitesLoaded = true;
        fetch(boot.sitesUrl)
            .then(function (r) {
                return r.json();
            })
            .then(function (data) {
                (data.sites || []).forEach(function (site) {
                    const option = document.createElement('option');
                    option.value = site;
                    siteDatalist.appendChild(option);
                });
            })
            .catch(function () {
                sitesLoaded = false;
            });
    }

    if (siteInput) {
        siteInput.addEventListener('change', function () {
            const value = this.value.trim();
            if (value) addSite(value);
            this.value = '';
        });
        siteInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const value = this.value.trim();
                if (value) addSite(value);
                this.value = '';
            }
        });
        siteInput.addEventListener('paste', function (e) {
            const paste = (e.clipboardData || window.clipboardData).getData('text');
            if (paste.indexOf(',') === -1) return;
            e.preventDefault();
            paste
                .split(',')
                .map(function (s) {
                    return s.trim();
                })
                .filter(Boolean)
                .forEach(addSite);
        });
    }

    if (selectedSitesContainer) {
        selectedSitesContainer.addEventListener('click', function (e) {
            const btn = e.target.closest('.chip-x');
            if (!btn) return;
            selectedSites.delete(btn.dataset.site);
            updateSiteHiddenInput();
            renderSites();
        });
    }

    window.maigretIcon = function (url) {
        try {
            const host = new URL(url).hostname;
            if (!host) return '';
            return 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=64';
        } catch (e) {
            return '';
        }
    };

    window.maigretSuggest = function (input, list, options) {
        let items = [];
        let active = -1;

        function close() {
            list.hidden = true;
            list.replaceChildren();
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            active = -1;
            items = [];
        }

        function render() {
            list.replaceChildren();
            if (!items.length) {
                close();
                return;
            }
            items.forEach(function (item, i) {
                const li = document.createElement('li');
                li.setAttribute('role', 'option');
                li.id = input.id + '-opt-' + i;
                li.className = 'suggest-opt' + (i === active ? ' is-active' : '') + (item.disabled ? ' is-disabled' : '');
                const icon = item.url ? window.maigretIcon(item.url) : '';
                if (icon) {
                    const img = document.createElement('img');
                    img.alt = '';
                    img.referrerPolicy = 'no-referrer';
                    img.src = icon;
                    img.addEventListener('error', function () { img.remove(); });
                    li.appendChild(img);
                }
                const label = document.createElement('span');
                label.textContent = item.name;
                li.appendChild(label);
                if (!item.disabled) {
                    li.addEventListener('mousedown', function (e) {
                        e.preventDefault();
                        choose(i);
                    });
                }
                list.appendChild(li);
            });
            list.hidden = false;
            input.setAttribute('aria-expanded', 'true');
            if (active >= 0) input.setAttribute('aria-activedescendant', input.id + '-opt-' + active);
        }

        function choose(i) {
            const item = items[i];
            if (!item || item.disabled) return;
            options.onPick(item);
            input.value = '';
            close();
        }

        function refresh() {
            const q = input.value.trim();
            if (!q) {
                close();
                return;
            }
            Promise.resolve(options.getItems(q)).then(function (next) {
                if (input.value.trim() !== q) return;
                items = (next || []).slice(0, 8);
                active = items.length && !items[0].disabled ? 0 : -1;
                render();
            });
        }

        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        input.setAttribute('aria-controls', list.id);
        input.addEventListener('input', refresh);
        input.addEventListener('focus', refresh);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown' && items.length) {
                e.preventDefault();
                active = Math.min(items.length - 1, active + 1);
                render();
            } else if (e.key === 'ArrowUp' && items.length) {
                e.preventDefault();
                active = Math.max(0, active - 1);
                render();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (active >= 0) choose(active);
            } else if (e.key === 'Escape') {
                close();
            }
        });
        input.addEventListener('blur', function () {
            setTimeout(close, 140);
        });
    };

    const PAGES_KEY = 'maigret-pages';

    function loadPages() {
        try {
            const data = JSON.parse(localStorage.getItem(PAGES_KEY) || '{}');
            return data && typeof data === 'object' ? data : {};
        } catch (e) {
            return {};
        }
    }

    function pageLines(usernames) {
        const data = loadPages();
        const names = usernames && usernames.length ? usernames : Object.keys(data);
        const lines = [];
        names.forEach(function (name) {
            const list = data[name] || [];
            if (!list.length) return;
            lines.push(name);
            list.forEach(function (item) {
                lines.push(item.site + '  ' + item.url);
            });
            lines.push('');
        });
        return lines.join('\n').trim();
    }

    window.maigretPages = {
        load: loadPages,
        save: function (data) {
            try {
                localStorage.setItem(PAGES_KEY, JSON.stringify(data || {}));
            } catch (e) {
                /* private mode */
            }
        },
        has: function (username, url) {
            return (loadPages()[username] || []).some(function (item) {
                return item.url === url;
            });
        },
        toggle: function (username, site, url, on) {
            const data = loadPages();
            const list = (data[username] || []).filter(function (item) {
                return item.url !== url;
            });
            if (on) list.push({ site: site, url: url });
            if (list.length) data[username] = list;
            else delete data[username];
            this.save(data);
            return data;
        },
        copy: function (usernames) {
            const text = pageLines(usernames);
            if (!text) return Promise.resolve(false);
            if (navigator.clipboard && navigator.clipboard.writeText) {
                return navigator.clipboard.writeText(text).then(
                    function () { return true; },
                    function () { return false; }
                );
            }
            return Promise.resolve(false);
        }
    };
})();
