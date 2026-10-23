import json

from maigret.web import names
from maigret.web.app import parse_usernames


def test_split_queries_keeps_a_full_name_together():
    assert names.split_queries('Kanye West') == ['Kanye West']
    assert names.split_queries('Kanye West, ada') == ['Kanye West', 'ada']
    assert names.split_queries('soxoj, alice') == ['soxoj', 'alice']


def test_parse_usernames_does_not_split_a_name_on_spaces():
    assert parse_usernames({'usernames': 'Kanye West'}) == ['Kanye West']
    assert parse_usernames({'usernames': 'soxoj'}) == ['soxoj']


def test_single_handle_is_not_a_person_name():
    assert names.is_person_name('kanyewest') is False
    assert names.is_person_name('Kanye West') is True
    assert names.name_handles('kanyewest') == []


def test_name_handles_use_the_whole_name():
    handles = names.name_handles('Kanye West')
    assert handles[0] == 'kanyewest'
    assert 'kanye.west' in handles
    assert 'kanye_west' in handles
    assert 'kanye-west' in handles
    assert 'kanye' not in handles
    assert 'west' not in handles


def test_wikidata_returns_public_pages_only(monkeypatch):
    search = {
        'search': [
            {'id': 'Q1', 'label': 'Kanye West', 'description': 'American rapper'}
        ]
    }
    entity = {
        'entities': {
            'Q1': {
                'descriptions': {'en': {'value': 'American rapper'}},
                'sitelinks': {'enwiki': {'title': 'Kanye West'}},
                'claims': {
                    'P569': [
                        {
                            'rank': 'preferred',
                            'mainsnak': {
                                'snaktype': 'value',
                                'datavalue': {'value': {'time': '+1977-06-08'}},
                            },
                        }
                    ],
                    'P2003': [
                        {
                            'rank': 'preferred',
                            'mainsnak': {
                                'snaktype': 'value',
                                'datavalue': {'value': 'kanyewest'},
                            },
                        }
                    ],
                },
            }
        }
    }

    def fake_get(url):
        if 'wbsearchentities' in url:
            return search
        return entity

    monkeypatch.setattr(names, '_get_json', fake_get)
    pages, about = names._wikidata_pages('Kanye West')
    dumped = json.dumps(pages)

    assert [page['site'] for page in pages] == ['Wikipedia', 'Instagram']
    assert pages[1]['url'] == 'https://www.instagram.com/kanyewest/'
    assert about == 'American rapper'
    assert '1977' not in dumped
