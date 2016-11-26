#!/usr/bin/env python
import re
import os
import time
import string
import requests

headers = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_7_3)'\
        'AppleWebKit/535.11 (KHTML, like Gecko) Chrome/17.0.963.79'\
        'Safari/535.11',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9'\
        ',*/*;q=0.8',
    'Accept-Encoding': 'gzip,deflate,sdch',
    'Accept-Language': 'en-US,en;q=0.8',
    'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.3'}

# gazelle is picky about case in searches with &media=x
media_search_map = {
    'cd': 'CD',
    'dvd': 'DVD',
    'vinyl': 'Vinyl',
    'soundboard': 'Soundboard',
    'sacd': 'SACD',
    'dat': 'DAT',
    'web': 'WEB',
    'blu-ray': 'Blu-ray'
}

lossless_media = set(media_search_map.keys())

def album_artists(album):
    return list(set(track.artist for track in album.tracks))

description_template = string.Template("""
[url=https://musicbrainz.org/release-group/$musicbrainz_id]MusicBrainz[/url]

Country: $country
Tracks: $num_tracks

Track list:
$track_list
""".strip())

track_template = string.Template("[#]$title")

def album_description(album):
    track_list = "\n".join([track_template.substitute(title=track.title) for track in album.tracks])
    return description_template.substitute(
        musicbrainz_id=album.album_id,
        country=album.country,
        num_tracks=len(album.tracks),
        track_list=track_list
    )

def create_upload_request(auth, album, torrent, logfiles, tags, artwork_url):
    artists = album_artists(album)
    data = {
        "auth": auth,  # input name=auth on upload page - appears to not change
        "type": 0,  # music
        "artists[]": artists,
        "importance[]": [0 for _ in artists],  # list of 0 for all main artists
        "title": album.album,
        "year": album.original_year,
        "record_label": "",  # optional
        "catalogue_number": "",  # optional
        "remaster": "on",  # if it's a remaster, off otherwise
        "remaster_title": "",  # optional
        "remaster_record_label": album.label,  # optional
        "remaster_catalogue_number": album.catalognum,  # optional
        "format": "FLAC",
        "bitrate": "Lossless",
        "other_bitrate": "",  # n/a
        "media": "CD",  # or WEB, Vinyl, etc. TODO: figure it out from album.media
        "genre_tags": tags[0],  # blank - this is the dropdown of official tags
        "tags": ", ".join(tags),  # classical, hip.hop, etc. (comma separated)
        "image": artwork_url,  # optional
        "album_desc": album_description(album),
        "release_desc": "Uploaded with [url=https://bitbucket.org/whatbetter/autotunes]autotunes[/url]."
    }
    files = [
        ("file_input", torrent),
    ]
    for logfile in logfiles:
        files.append(("logfiles[]", logfile))
    return data, files

class LoginException(Exception):
    pass

class RequestException(Exception):
    pass

class WhatAPI:
    def __init__(self, username=None, password=None):
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.username = username
        self.password = password
        self.authkey = None
        self.passkey = None
        self.userid = None
        self.tracker = "https://please.passtheheadphones.me/"
        self.last_request = time.time()
        self.rate_limit = 2.0 # seconds between requests
        self._login()

    def _login(self):
        '''Logs in user and gets authkey from server'''
        loginpage = 'https://passtheheadphones.me/login.php'
        data = {'username': self.username,
                'password': self.password}
        r = self.session.post(loginpage, data=data)
        if r.status_code != 200:
            raise LoginException
        accountinfo = self.request('index')
        self.authkey = accountinfo['authkey']
        self.passkey = accountinfo['passkey']
        self.userid = accountinfo['id']

    def logout(self):
        self.session.get("https://passtheheadphones.me/logout.php?auth=%s" % self.authkey)

    def request(self, action, **kwargs):
        '''Makes an AJAX request at a given action page'''
        while time.time() - self.last_request < self.rate_limit:
            time.sleep(0.1)

        ajaxpage = 'https://passtheheadphones.me/ajax.php'
        params = {'action': action}
        if self.authkey:
            params['auth'] = self.authkey
        params.update(kwargs)
        r = self.session.get(ajaxpage, params=params, allow_redirects=False)
        self.last_request = time.time()
        response = r.json()
        if response['status'] != 'success':
            raise RequestException
        return response['response']

    def get_artist(self, id=None, format='MP3', best_seeded=True):
        res = self.request('artist', id=id)
        torrentgroups = res['torrentgroup']
        keep_releases = []
        for release in torrentgroups:
            torrents = release['torrent']
            best_torrent = torrents[0]
            keeptorrents = []
            for t in torrents:
                if t['format'] == format:
                    if best_seeded:
                        if t['seeders'] > best_torrent['seeders']:
                            keeptorrents = [t]
                            best_torrent = t
                    else:
                        keeptorrents.append(t)
            release['torrent'] = list(keeptorrents)
            if len(release['torrent']):
                keep_releases.append(release)
        res['torrentgroup'] = keep_releases
        return res

    def snatched(self, skip=None, media=lossless_media):
        if not media.issubset(lossless_media):
            raise ValueError('Unsupported media type %s' % (media - lossless_media).pop())

        # gazelle doesn't currently support multiple values per query
        # parameter, so we have to search a media type at a time;
        # unless it's all types, in which case we simply don't specify
        # a 'media' parameter (defaults to all types).

        if media == lossless_media:
            media_params = ['']
        else:
            media_params = ['&media=%s' % media_search_map[m] for m in media]

        url = 'https://passtheheadphones.me/torrents.php?type=snatched&userid=%s&format=FLAC' % self.userid
        for mp in media_params:
            page = 1
            done = False
            pattern = re.compile('torrents.php\?id=(\d+)&amp;torrentid=(\d+)')
            while not done:
                content = self.session.get(url + mp + "&page=%s" % page).text
                for groupid, torrentid in pattern.findall(content):
                    if skip is None or torrentid not in skip:
                        yield int(groupid), int(torrentid)
                done = 'Next &gt;' not in content
                page += 1

    def upload(self, auth, album, torrent, logfiles, tags, artwork_url):
        url = "http://requestb.in/1ktwnu81"
        data, files = create_upload_request(auth, album, torrent, logfiles, tags, artwork_url)

        # post as multipart/form-data
        return self.session.post(url, data=data, files=files, headers=dict(headers))

    def release_url(self, group, torrent):
        return "https://passtheheadphones.me/torrents.php?id=%s&torrentid=%s#torrent%s" % (group['group']['id'], torrent['id'], torrent['id'])

    def permalink(self, torrent):
        return "https://passtheheadphones.me/torrents.php?torrentid=%s" % torrent['id']