# Copyright: 2011, Grigoriy Petukhov
# Author: Grigoriy Petukhov (http://lorien.name)
# License: BSD
from __future__ import absolute_import
import email
import logging
import urllib
from StringIO import StringIO
import threading
import random
from urlparse import urlsplit, urlunsplit
import pycurl

from ..base import UploadContent, UploadFile
from .. import error
from ..response import Response
from ..tools.http import encode_cookies, urlencode, normalize_unicode,\
                         normalize_http_values

logger = logging.getLogger('grab.transport.curl')

# @lorien: I do not understand these signals. Maybe you?

# We should ignore SIGPIPE when using pycurl.NOSIGNAL - see
# the libcurl tutorial for more info.

# http://curl.haxx.se/mail/curlpython-2005-06/0004.html
# http://curl.haxx.se/mail/lib-2010-03/0114.html

#CURLOPT_NOSIGNAL
#Pass a long. If it is 1, libcurl will not use any functions that install
# signal handlers or any functions that cause signals to be sent to the
# process. This option is mainly here to allow multi-threaded unix applications
# to still set/use all timeout options etc, without risking getting signals.
# (Added in 7.10)

#If this option is set and libcurl has been built with the standard name
# resolver, timeouts will not occur while the name resolve takes place.
# Consider building libcurl with c-ares support to enable asynchronous DNS 
# lookups, which enables nice timeouts for name resolves without signals.

try:
    import signal
    from signal import SIGPIPE, SIG_IGN
    try:
        signal.signal(SIGPIPE, SIG_IGN)
    except ValueError:
        # Ignore the exception
        # ValueError: signal only works in main thread
        pass
except ImportError:
    pass


class CurlTransport(object):
    """
    Grab transport layer using pycurl.
    """

    def reset(self):
        self.response_head_chunks = []
        self.response_body_chunks = []
        self.response_body_bytes_read = 0
        self.verbose_logging = False

        # Maybe move to super-class???
        self.request_headers = ''
        self.request_head = ''
        self.request_body = ''
        self.request_log = ''

    def head_processor(self, chunk):
        """
        Process head of response.
        """

        #if self.config['nohead']:
            #return 0
        self.response_head_chunks.append(chunk)
        # Returning None implies that all bytes were written
        return None

    def body_processor(self, chunk):
        """
        Process body of response.
        """

        if self.config_nobody:
            return 0

        bytes_read = len(chunk)
        self.response_body_bytes_read += bytes_read
        self.response_body_chunks.append(chunk)
        if self.config_body_maxsize is not None:
            if self.response_body_bytes_read > self.config_body_maxsize:
                logger.debug('Response body max size limit reached: %s' %
                             self.config_body_maxsize)
                return 0

        # Returning None implies that all bytes were written
        return None

    def debug_processor(self, _type, text):
        """
        Parse request headers and save to ``self.request_headers``

        0: CURLINFO_TEXT
        1: CURLINFO_HEADER_IN
        2: CURLINFO_HEADER_OUT
        3: CURLINFO_DATA_IN
        4: CURLINFO_DATA_OUT
        5: CURLINFO_unrecognized_type
        """

        if _type == pycurl.INFOTYPE_HEADER_OUT:
            self.request_head += text
            lines = text.splitlines()
            merged_lines = '\n'.join(lines[1:])
            self.request_headers = email.message_from_string(merged_lines)

        if _type == pycurl.INFOTYPE_DATA_OUT:
            self.request_body += text

        if _type == pycurl.INFOTYPE_TEXT:
            if self.request_log is None:
                self.request_log = ''
            self.request_log += text

        if self.verbose_logging:
            if _type in (pycurl.INFOTYPE_TEXT, pycurl.INFOTYPE_HEADER_IN,
                         pycurl.INFOTYPE_HEADER_OUT):
                marker_types = {
                    pycurl.INFOTYPE_TEXT: 'i',
                    pycurl.INFOTYPE_HEADER_IN: '<',
                    pycurl.INFOTYPE_HEADER_OUT: '>',
                }
                marker = marker_types[_type]
                logger.debug('%s: %s' % (marker, text.rstrip()))

    def process_config(self, grab):
        """
        Setup curl instance with values from ``self.config``.
        """

        try:
            self.curl
        except NameError:
            self.curl = pycurl.Curl()

        # Copy some config for future usage
        self.config_nobody = grab.config['nobody']
        self.config_body_maxsize = grab.config['body_maxsize']

        request_url = grab.config['url']
        if isinstance(request_url, unicode):
            request_url = request_url.encode('utf-8')
        self.curl.setopt(pycurl.URL, request_url)

        self.curl.setopt(pycurl.FOLLOWLOCATION, 1 if grab.config['follow_location'] else 0)
        self.curl.setopt(pycurl.MAXREDIRS, grab.config['redirect_limit'])
        self.curl.setopt(pycurl.CONNECTTIMEOUT, grab.config['connect_timeout'])
        self.curl.setopt(pycurl.TIMEOUT, grab.config['timeout'])

        self.curl.setopt(pycurl.NOSIGNAL, 1)
        self.curl.setopt(pycurl.WRITEFUNCTION, self.body_processor)
        self.curl.setopt(pycurl.HEADERFUNCTION, self.head_processor)

        if grab.config['verbose_logging']:
            self.verbose_logging = True

        # User-Agent
        if grab.config['user_agent'] is None:
            if grab.config['user_agent_file'] is not None:
                with open(grab.config['user_agent_file']) as inf:
                    lines = inf.read().splitlines()
                grab.config['user_agent'] = random.choice(lines)

        # If value is None then set empty string
        # None is not acceptable because in such case
        # pycurl will set its default user agent "PycURL/x.xx.x"
        if not grab.config['user_agent']:
            grab.config['user_agent'] = ''

        self.curl.setopt(pycurl.USERAGENT, grab.config['user_agent'])

        self.curl.setopt(pycurl.VERBOSE, 1)
        self.curl.setopt(pycurl.DEBUGFUNCTION, self.debug_processor)

        # Ignore SSL errors
        self.curl.setopt(pycurl.SSL_VERIFYPEER, 0)
        self.curl.setopt(pycurl.SSL_VERIFYHOST, 0)

        if grab.request_method == 'POST':
            self.curl.setopt(pycurl.POST, 1)
            if grab.config['multipart_post']:
                if isinstance(grab.config['multipart_post'], basestring):
                    raise error.GrabMisuseError('multipart_post option could not be a string')
                post_items = normalize_http_values(grab.config['multipart_post'],
                                                   charset=grab.charset)
                self.curl.setopt(pycurl.HTTPPOST, post_items) 
            elif grab.config['post']:
                if isinstance(grab.config['post'], basestring):
                    # bytes-string should be posted as-is
                    # unicode should be converted into byte-string
                    if isinstance(grab.config['post'], unicode):
                        post_data = normalize_unicode(grab.config['post'])
                    else:
                        post_data = grab.config['post']
                else:
                    # dict, tuple, list should be serialized into byte-string
                    post_data = urlencode(grab.config['post'])
                self.curl.setopt(pycurl.POSTFIELDS, post_data)
            else:
                self.curl.setopt(pycurl.POSTFIELDS, '')
        elif grab.request_method == 'PUT':
            self.curl.setopt(pycurl.PUT, 1)
            self.curl.setopt(pycurl.READFUNCTION, StringIO(grab.config['post']).read) 
        elif grab.request_method == 'DELETE':
            self.curl.setopt(pycurl.CUSTOMREQUEST, 'delete')
        elif grab.request_method == 'HEAD':
            self.curl.setopt(pycurl.NOBODY, 1)
        elif grab.request_method == 'UPLOAD':
            self.curl.setopt(pycurl.UPLOAD, 1)
        else:
            self.curl.setopt(pycurl.HTTPGET, 1)
        
        headers = grab.default_headers
        if grab.config['headers']:
            headers.update(grab.config['headers'])
        header_tuples = [str('%s: %s' % x) for x\
                         in headers.iteritems()]
        self.curl.setopt(pycurl.HTTPHEADER, header_tuples)


        # CURLOPT_COOKIELIST
        # Pass a char * to a cookie string. Cookie can be either in
        # Netscape / Mozilla format or just regular HTTP-style
        # header (Set-Cookie: ...) format.
        # If cURL cookie engine was not enabled it will enable its cookie
        # engine.
        # Passing a magic string "ALL" will erase all cookies known by cURL.
        # (Added in 7.14.1)
        # Passing the special string "SESS" will only erase all session
        # cookies known by cURL. (Added in 7.15.4)
        # Passing the special string "FLUSH" will write all cookies known by
        # cURL to the file specified by CURLOPT_COOKIEJAR. (Added in 7.17.1)

        # CURLOPT_COOKIE
        # Pass a pointer to a zero terminated string as parameter. It will be used to set a cookie in the http request. The format of the string should be NAME=CONTENTS, where NAME is the cookie name and CONTENTS is what the cookie should contain.
        # If you need to set multiple cookies, you need to set them all using a single option and thus you need to concatenate them all in one single string. Set multiple cookies in one string like this: "name1=content1; name2=content2;" etc.
        # Note that this option sets the cookie header explictly in the outgoing request(s). If multiple requests are done due to authentication, followed redirections or similar, they will all get this cookie passed on.
        # Using this option multiple times will only make the latest string override the previous ones. 

        # `cookiefile` option shoul be processed before `cookies` option
        # because `load_cookies` updates `cookies` option
        if grab.config['cookiefile']:
            grab.load_cookies(grab.config['cookiefile'])

        if grab.config['cookies']:
            if not isinstance(grab.config['cookies'], dict):
                raise error.GrabMisuseError('cookies option shuld be a dict')
            items = encode_cookies(grab.config['cookies'], join=False)
            self.curl.setopt(pycurl.COOKIELIST, 'ALL')
            for item in items:
                self.curl.setopt(pycurl.COOKIELIST, 'Set-Cookie: %s' % item)
        else:
            # Turn on cookies engine anyway
            # To correctly support cookies in 302-redirects
            self.curl.setopt(pycurl.COOKIEFILE, '')

        if grab.config['referer']:
            self.curl.setopt(pycurl.REFERER, str(grab.config['referer']))

        if grab.config['proxy']:
            self.curl.setopt(pycurl.PROXY, str(grab.config['proxy'])) 
        else:
            self.curl.setopt(pycurl.PROXY, '')

        if grab.config['proxy_userpwd']:
            self.curl.setopt(pycurl.PROXYUSERPWD, grab.config['proxy_userpwd'])

        # PROXYTYPE
        # Pass a long with this option to set type of the proxy. Available options for this are CURLPROXY_HTTP, CURLPROXY_HTTP_1_0 (added in 7.19.4), CURLPROXY_SOCKS4 (added in 7.15.2), CURLPROXY_SOCKS5, CURLPROXY_SOCKS4A (added in 7.18.0) and CURLPROXY_SOCKS5_HOSTNAME (added in 7.18.0). The HTTP type is default. (Added in 7.10) 

        if grab.config['proxy_type']:
            ptype = getattr(pycurl, 'PROXYTYPE_%s' % grab.config['proxy_type'].upper())
            self.curl.setopt(pycurl.PROXYTYPE, ptype)

        if grab.config['encoding']:
            if 'gzip' in grab.config['encoding'] and not 'zlib' in pycurl.version:
                raise error.GrabMisuseError('You can not use gzip encoding because '\
                                      'pycurl was built without zlib support')
            self.curl.setopt(pycurl.ENCODING, grab.config['encoding'])

        if grab.config['userpwd']:
            self.curl.setopt(pycurl.USERPWD, grab.config['userpwd'])

        if grab.config['charset']:
            self.charset = grab.config['charset']

    def request(self):

        try:
            self.curl.perform()
        except pycurl.error, ex:
            # CURLE_WRITE_ERROR
            # An error occurred when writing received data to a local file, or
            # an error was returned to libcurl from a write callback.
            # This is expected error and we should ignore it
            #
            # TODO: maybe is should be ignored only in case of using
            # nobody and body_maxsize options
            if 23 == ex[0]:
                pass
            else:
                if ex[0] == 28:
                    raise error.GrabTimeoutError(ex[0], ex[1])
                elif ex[0] == 7:
                    raise error.GrabConnectionError(ex[0], ex[1])
                elif ex[0] == 67:
                    raise error.GrabAuthError(ex[0], ex[1])
                else:
                    raise error.GrabNetworkError(ex[0], ex[1])

    def prepare_response(self, grab):
        response = Response()
        response.head = ''.join(self.response_head_chunks)
        response.body = ''.join(self.response_body_chunks)
        response.code = self.curl.getinfo(pycurl.HTTP_CODE)
        response.time = self.curl.getinfo(pycurl.TOTAL_TIME)
        response.url = self.curl.getinfo(pycurl.EFFECTIVE_URL)

        if grab.config['charset'] is not None:
            response.parse(charset=grab.config['charset'])
        else:
            response.parse()

        response.cookies = self.extract_cookies()

        # We do not need anymore cookies stored in the
        # curl instance so drop them
        self.curl.setopt(pycurl.COOKIELIST, 'ALL')
        return response

    def extract_cookies(self):
        """
        Extract cookies.
        """

        # Example of line:
        # www.google.com\tFALSE\t/accounts/\tFALSE\t0\tGoogleAccountsLocale_session\ten
        cookies = {}
        for line in self.curl.getinfo(pycurl.INFO_COOKIELIST):
            chunks = line.split('\t')
            cookies[chunks[-2]] = chunks[-1]
        return cookies



#from ..base import BaseGrab
#class GrabCurl(CurlTransportExtension, BaseGrab):
    #pass
