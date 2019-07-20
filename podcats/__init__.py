"""
Podcats is a podcast feed generator and a server.

It generates RSS feeds for podcast episodes from local audio files and,
optionally, exposes the feed and as well as the episode file via
a built-in web server so that they can be imported into iTunes
or another podcast client.

"""
import os
import re
import time
import argparse
import mimetypes
import json
from urllib.parse import urlparse
from email.utils import formatdate
from os import path
from xml.sax.saxutils import escape, quoteattr

try:
    from urllib.request import pathname2url
except ImportError:
    # For python 2
    # noinspection PyUnresolvedReferences
    from urllib import pathname2url

import mutagen
import humanize
from mutagen.id3 import ID3
from flask import Flask, Response
# noinspection PyPackageRequirements
from jinja2 import Environment, FileSystemLoader
import boto3

s3 = boto3.resource('s3')
s3_client = boto3.client('s3')

__version__ = '0.6.3'
__licence__ = 'BSD'
__author__ = 'Jakub Roztocil'
__url__ = 'https://github.com/jakubroztocil/podcats'


WEB_PATH = '/web'
STATIC_PATH = '/static'
TEMPLATES_ROOT = os.path.join(os.path.dirname(__file__), 'templates')
BOOK_COVER_EXTENSIONS = ('.jpg', '.jpeg', '.png')

jinja2_env = Environment(loader=FileSystemLoader(TEMPLATES_ROOT))


class Episode:
    """Podcast episode"""

    def __init__(self, filename, relative_dir, root_url):
        self.filename = filename
        self.relative_dir = relative_dir
        self.root_url = root_url
        self.length = self.get_length()
        self.tags = self.get_tags()
        try:
            self.id3 = ID3(self.filename)
        except Exception:
            self.id3 = None

    def get_length(self):
        return os.path.getsize(self.filename)

    def get_tags(self):
        return mutagen.File(self.filename, easy=True)

    def __lt__(self, other):
        return self.date < other.date

    def __gt__(self, other):
        return self.date > other.date

    def __cmp__(self, other):
        a, b = self.date, other.date
        return (a > b) - (a < b)  # Python3 cmp() equivalent

    def as_xml(self):
        """Return episode item XML"""
        template = jinja2_env.get_template('episode.xml')

        return template.render(
            title=escape(self.title),
            url=quoteattr(self.url),
            guid=escape(self.url),
            mimetype=self.mimetype,
            length=self.length,
            date=formatdate(self.date),
            image_url=self.image,
            speaker=escape(self.speaker),
            description=escape(self.description)
        )

    def as_html(self):
        """Return episode item html"""
        filename = os.path.basename(self.filename)
        directory = os.path.split(os.path.dirname(self.filename))[-1]
        template = jinja2_env.get_template('episode.html')

        return template.render(
            title=escape(self.title),
            url=self.url,
            filename=filename,
            directory=directory,
            mimetype=self.mimetype,
            length=humanize.naturalsize(self.length),
            date=formatdate(self.date),
            image_url=self.image,
            speaker=escape(self.speaker),
            description=escape(self.description)
        )

    def get_tag(self, name):
        """Return episode file tag info"""
        try:
            return self.tags[name][0]
        except (KeyError, IndexError):
            pass

    def _to_url(self, filepath):
        fn = os.path.basename(filepath)
        path = STATIC_PATH + '/' + self.relative_dir + '/' + fn
        path = re.sub(r'//', '/', path)
        url = self.root_url + pathname2url(path)
        return url

    @property
    def title(self):
        """Return episode title"""
        text = os.path.splitext(os.path.basename(self.filename))[0]
        if self.id3 is not None:
            val = self.id3.getall('TIT2')
            if len(val) > 0:
                text += str(val[0])
            val = self.id3.getall('COMM')
            if len(val) > 0:
                text += ' ' + str(val[0])
        return text

    @property
    def url(self):
        """Return episode url"""
        return self._to_url(self.filename)

    @property
    def date(self):
        """Return episode date as unix timestamp"""
        dt = self.get_tag('date')
        if dt:
            formats = [
                '%Y-%m-%d:%H:%M:%S',
                '%Y-%m-%d:%H:%M',
                '%Y-%m-%d:%H',
                '%Y-%m-%d',
                '%Y-%m',
                '%Y',
            ]
            for fmt in formats:
                try:
                    dt = time.mktime(time.strptime(dt, fmt))
                    break
                except ValueError:
                    pass
            else:
                dt = None

        if not dt:
            dt = os.path.getmtime(self.filename)

        return dt

    @property
    def mimetype(self):
        """Return file mimetype name"""
        if self.filename.endswith('m4b'):
            return 'audio/x-m4b'
        else:
            return mimetypes.guess_type(self.filename)[0]

    @property
    def image(self):
        """Return an eventual cover image"""
        directory = os.path.split(self.filename)[0]
        image_files = []

        for fn in os.listdir(directory):
            ext = os.path.splitext(fn)[1]
            if ext.lower() in BOOK_COVER_EXTENSIONS:
                image_files.append(fn)

        if len(image_files) > 0:
            abs_path_image = image_files[0]
            return self._to_url(abs_path_image)
        else:
            return None

    @property
    def description(self):
        """Return episode description"""
        return None

    @property
    def speaker(self):
        """Return episode speaker"""
        return None


class S3Episode(Episode):
    _data = None

    def get_length(self):
        return self.filename.size

    def get_tags(self):
        path = self.filename.key.split('/')
        return {'date': ['-'.join(path[:3])]}

    @property
    def image(self):
        return None

    @property
    def mimetype(self):
        """Return file mimetype name"""
        return mimetypes.guess_type(self.filename.key)[0]

    @property
    def s3_metadata(self):
        if self._data is None:
            path_parts = self.filename.key.split('/')
            prefix = '/'.join(path_parts[:3])
            for obj in s3.Bucket(self.filename.bucket_name).objects.filter(Prefix=prefix):
                if obj.key.endswith('json'):
                    data = json.load(obj.get()['Body'])
                    # check we have the correct json file
                    if path_parts[-1] in data.values():
                        self._data = data
        return self._data

    @property
    def title(self):
        """Return episode title"""
        return self.s3_metadata['Recording Title']

    @property
    def description(self):
        """Return episode description"""
        return self.s3_metadata['Description']

    @property
    def speaker(self):
        """Return episode speaker"""
        return self.s3_metadata['Speaker']

    @property
    def url(self):
        bucket_location = s3_client.get_bucket_location(
            Bucket=self.filename.bucket_name
        )
        return "https://s3-{0}.amazonaws.com/{1}/{2}".format(
            bucket_location['LocationConstraint'],
            self.filename.bucket_name,
            self.filename.key
        )


class Channel:
    """Podcast channel"""

    def __init__(self, root_dir, root_url, host, port, title, link, debug=False):
        if self._is_s3(root_dir):
            self.root_dir = root_dir
        else:
            self.root_dir = path.abspath(root_dir or os.getcwd())
        self.root_url = root_url
        self.host = host
        self.port = int(port)
        self.link = link or self.root_url
        self.title = title or os.path.basename(os.path.abspath(self.root_dir.rstrip('/')))
        self.description = 'Feed generated by <a href="%s">Podcats</a>.' % __url__
        self.debug = debug

    def _is_s3(self, root_dir):
        return all(i in root_dir for i in ['s3', 'amazonaws.com'])

    def __iter__(self):
        if self._is_s3(self.root_dir):
            for file in self._iter_s3():
                yield file
        else:
            for file in self._iter_local():
                yield file

    def _iter_s3(self):
        parts = urlparse(self.root_dir)
        bucket, _, region, _, _ = parts.netloc.split('.')
        for obj in s3.Bucket(bucket).objects.filter(Prefix=parts.path):
            if obj.key.endswith('mp3'):
                relative_dir = '/'.join(obj.key.split('/')[:-1])
                yield S3Episode(obj,
                                relative_dir, self.root_url)


    def _iter_local(self):
        for root, _, files in os.walk(self.root_dir):
            relative_dir = root[len(self.root_dir):]
            for fn in files:
                filepath = os.path.join(root, fn)
                mimetype = mimetypes.guess_type(filepath)[0]
                if mimetype and 'audio' in mimetype or filepath.endswith('m4b'):
                    yield Episode(filepath, relative_dir, self.root_url)

    def as_xml(self):
        """Return channel XML with all episode items"""
        template = jinja2_env.get_template('feed.xml')
        return template.render(
            title=escape(self.title),
            description=escape(self.description),
            link=escape(self.link),
            items=u''.join(episode.as_xml() for episode in sorted(self))
        ).strip()

    def as_html(self):
        """Return channel HTML with all episode items"""
        template = jinja2_env.get_template('feed.html')
        return template.render(
            title=escape(self.title),
            description=self.description,
            link=escape(self.link),
            items=u''.join(episode.as_html() for episode in sorted(self)),
        ).strip()


def serve(channel):
    """Serve podcast channel and episodes over HTTP"""
    server = Flask(
        __name__,
        static_folder=channel.root_dir,
        static_url_path=STATIC_PATH,
    )
    server.route('/')(
        lambda: Response(
            channel.as_xml(),
            content_type='application/xml; charset=utf-8')
    )
    server.add_url_rule(
        WEB_PATH,
        view_func=channel.as_html,
        methods=['GET'],
    )
    server.run(host=channel.host, port=channel.port, debug=channel.debug, threaded=True)


def main():
    """Main function"""
    args = parser.parse_args()
    url = 'http://' + args.host + ':' + args.port
    channel = Channel(
        root_dir=args.directory,
        root_url=url,
        host=args.host,
        port=args.port,
        title=args.title,
        link=args.link,
        debug=args.debug,
    )
    if args.action == 'generate':
        print(channel.as_xml())
    elif args.action == 'generate_html':
        print(channel.as_html())
    else:
        print('Welcome to the Podcats web server!')
        print('\nYour podcast feed is available at:\n')
        print('\t' + channel.root_url + '\n')
        print('The web interface is available at\n')
        print('\t{url}{web_path}\n'.format(url=url, web_path=WEB_PATH))
        serve(channel)


parser = argparse.ArgumentParser(
    description='Podcats: podcast feed generator and server <%s>.' % __url__
)
parser.add_argument(
    '--host',
    default='localhost',
    help='listen hostname or IP address'
)
parser.add_argument(
    '--port',
    default='5000',
    help='listen tcp port number'
)
parser.add_argument(
    'action',
    metavar='COMMAND',
    choices=['generate', 'generate_html', 'serve'],
    help='`generate` the RSS feed to the terminal, or'
         '`serve` the generated RSS as well as audio files'
         ' via the built-in web server'
)
parser.add_argument(
    'directory',
    metavar='DIRECTORY',
    help='path to a directory with episode audio files',
)
parser.add_argument(
    '--debug',
    action="store_true",
    help='Serve with debug mode on'
)
parser.add_argument('--title', help='optional feed title')
parser.add_argument('--link', help='optional feed link')


if __name__ == '__main__':
    main()
