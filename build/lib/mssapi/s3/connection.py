# Copyright (c) 2006-2012 Mitch Garnaat http://garnaat.org/
# Copyright (c) 2012 Amazon.com, Inc. or its affiliates.
# Copyright (c) 2010, Eucalyptus Systems, Inc.
# All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish, dis-
# tribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the fol-
# lowing conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABIL-
# ITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
# SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import xml.sax
import base64
from mssapi.compat import six, urllib
import time

from mssapi.auth import detect_potential_s3sigv4
import mssapi.utils
from mssapi.connection import AWSAuthConnection
from mssapi import handler
from mssapi.s3.bucket import Bucket
from mssapi.s3.key import Key
from mssapi.resultset import ResultSet
from mssapi.exception import MssapiClientError, S3ResponseError


def check_lowercase_bucketname(n):
    """
    Bucket names must not contain uppercase characters. We check for
    this by appending a lowercase character and testing with islower().
    Note this also covers cases like numeric bucket names with dashes.

    >>> check_lowercase_bucketname("Aaaa")
    Traceback (most recent call last):
    ...
    MssapiClientError: S3Error: Bucket names cannot contain upper-case
    characters when using either the sub-domain or virtual hosting calling
    format.

    >>> check_lowercase_bucketname("1234-5678-9123")
    True
    >>> check_lowercase_bucketname("abcdefg1234")
    True
    """
    if not (n + 'a').islower():
        raise MssapiClientError("Bucket names cannot contain upper-case " \
            "characters when using either the sub-domain or virtual " \
            "hosting calling format.")
    return True


def assert_case_insensitive(f):
    def wrapper(*args, **kwargs):
        if len(args) == 3 and check_lowercase_bucketname(args[2]):
            pass
        return f(*args, **kwargs)
    return wrapper


class _CallingFormat(object):

    def get_bucket_server(self, server, bucket):
        return ''

    def build_url_base(self, connection, protocol, server, bucket, key=''):
        url_base = '%s://' % protocol
        url_base += self.build_host(server, bucket)
        url_base += connection.get_path(self.build_path_base(bucket, key))
        return url_base

    def build_host(self, server, bucket):
        if bucket == '':
            return server
        else:
            return self.get_bucket_server(server, bucket)

    def build_auth_path(self, bucket, key=''):
        key = mssapi.utils.get_utf8_value(key)
        path = ''
        if bucket != '':
            path = '/' + bucket
        return path + '/%s' % urllib.parse.quote(key)

    def build_path_base(self, bucket, key=''):
        key = mssapi.utils.get_utf8_value(key)
        return '/%s' % urllib.parse.quote(key)


class SubdomainCallingFormat(_CallingFormat):

    @assert_case_insensitive
    def get_bucket_server(self, server, bucket):
        return '%s.%s' % (bucket, server)


class VHostCallingFormat(_CallingFormat):

    @assert_case_insensitive
    def get_bucket_server(self, server, bucket):
        return bucket


class OrdinaryCallingFormat(_CallingFormat):

    def get_bucket_server(self, server, bucket):
        return server

    def build_path_base(self, bucket, key=''):
        key = mssapi.utils.get_utf8_value(key)
        path_base = '/'
        if bucket:
            path_base += "%s/" % bucket
        return path_base + urllib.parse.quote(key)


class ProtocolIndependentOrdinaryCallingFormat(OrdinaryCallingFormat):

    def build_url_base(self, connection, protocol, server, bucket, key=''):
        url_base = '//'
        url_base += self.build_host(server, bucket)
        url_base += connection.get_path(self.build_path_base(bucket, key))
        return url_base


class Location(object):

    DEFAULT = ''  # US Classic Region
    EU = 'EU'
    USWest = 'us-west-1'
    USWest2 = 'us-west-2'
    SAEast = 'sa-east-1'
    APNortheast = 'ap-northeast-1'
    APSoutheast = 'ap-southeast-1'
    APSoutheast2 = 'ap-southeast-2'
    CNNorth1 = 'cn-north-1'


class NoHostProvided(object):
    # An identifying object to help determine whether the user provided a
    # ``host`` or not. Never instantiated.
    pass


class HostRequiredError(MssapiClientError):
    pass

class NotSupportError( Exception ): pass

class S3Connection(AWSAuthConnection):

    DefaultHost = mssapi.config.get('s3', 'host', 'mtmos.com')
    DefaultCallingFormat = OrdinaryCallingFormat()
    QueryString = 'Signature=%s&Expires=%d&AWSAccessKeyId=%s'

    def __init__(self, aws_access_key_id=None, aws_secret_access_key=None,
                 is_secure=False, port=None, host=NoHostProvided,
                 calling_format=DefaultCallingFormat, path='/',
                 suppress_consec_slashes=True):

        proxy=None
        proxy_port=None
        proxy_user=None
        proxy_pass=None
        debug = 0
        https_connection_factory=None
        provider='aws'
        bucket_class=Bucket
        security_token=None
        anon=False
        validate_certs=None
        profile_name=None


        no_host_provided = False
        if host is NoHostProvided:
            no_host_provided = True
            host = self.DefaultHost

        if isinstance(calling_format, six.string_types):
            calling_format=mssapi.utils.find_class(calling_format)()
        self.calling_format = calling_format
        self.bucket_class = bucket_class
        self.anon = anon

        super(S3Connection, self).__init__(host,
                aws_access_key_id, aws_secret_access_key,
                is_secure, port, proxy, proxy_port, proxy_user, proxy_pass,
                debug=debug, https_connection_factory=https_connection_factory,
                path=path, provider=provider, security_token=security_token,
                suppress_consec_slashes=suppress_consec_slashes,
                validate_certs=validate_certs, profile_name=profile_name)
        # We need to delay until after the call to ``super`` before checking
        # to see if SigV4 is in use.
        if no_host_provided:
            if 'hmac-v4-s3' in self._required_auth_capability():
                raise HostRequiredError(
                    "When using SigV4, you must specify a 'host' parameter."
                )

    @detect_potential_s3sigv4
    def _required_auth_capability(self):
        if self.anon:
            return ['anon']
        else:
            return ['s3']

    def __iter__(self):
        for bucket in self.get_all_buckets():
            yield bucket

    def __contains__(self, bucket_name):
        return not (self.lookup(bucket_name) is None)

    def generate_url(self, expires_in, method, bucket='', key='', headers=None,
                     query_auth=True, force_http=False, expires_in_absolute=False):

        version_id=None
        response_headers=None

        if self._auth_handler.capability[0] == 'hmac-v4-s3':
            # Handle the special sigv4 case
            return self.generate_url_sigv4(expires_in, method, bucket=bucket,
                key=key, headers=headers, force_http=force_http,
                response_headers=response_headers, version_id=version_id)

        headers = headers or {}
        if expires_in_absolute:
            expires = int(expires_in)
        else:
            expires = int(time.time() + expires_in)
        auth_path = self.calling_format.build_auth_path(bucket, key)
        auth_path = self.get_path(auth_path)
        # optional version_id and response_headers need to be added to
        # the query param list.
        extra_qp = []
        if version_id is not None:
            extra_qp.append("versionId=%s" % version_id)
        if response_headers:
            for k, v in response_headers.items():
                extra_qp.append("%s=%s" % (k, urllib.parse.quote(v)))
        if self.provider.security_token:
            headers['x-amz-security-token'] = self.provider.security_token
        if extra_qp:
            delimiter = '?' if '?' not in auth_path else '&'
            auth_path += delimiter + '&'.join(extra_qp)
        c_string = mssapi.utils.canonical_string(method, auth_path, headers,
                                               expires, self.provider)
        b64_hmac = self._auth_handler.sign_string(c_string)
        encoded_canonical = urllib.parse.quote(b64_hmac, safe='')
        self.calling_format.build_path_base(bucket, key)
        if query_auth:
            query_part = '?' + self.QueryString % (encoded_canonical, expires,
                                                   self.aws_access_key_id)
        else:
            query_part = ''
        if headers:
            hdr_prefix = self.provider.header_prefix
            for k, v in headers.items():
                if k.startswith(hdr_prefix):
                    # headers used for sig generation must be
                    # included in the url also.
                    extra_qp.append("%s=%s" % (k, urllib.parse.quote(v)))
        if extra_qp:
            delimiter = '?' if not query_part else '&'
            query_part += delimiter + '&'.join(extra_qp)
        if force_http:
            protocol = 'http'
            port = 80
        else:
            protocol = self.protocol
            port = self.port
        return self.calling_format.build_url_base(self, protocol,
                                                  self.server_name(port),
                                                  bucket, key) + query_part

    def get_all_buckets(self, headers=None):
        response = self.make_request('GET', headers=headers)
        body = response.read()
        if response.status > 300:
            raise self.provider.storage_response_error(
                response.status, response.reason, body)
        rs = ResultSet([('Bucket', self.bucket_class)])
        h = handler.XmlHandler(rs, self)
        if not isinstance(body, bytes):
            body = body.encode('utf-8')
        xml.sax.parseString(body, h)
        return rs

    def get_bucket(self, bucket_name, validate=True, headers=None):
        """
        Retrieves a bucket by name.

        If the bucket does not exist, an ``S3ResponseError`` will be raised. If
        you are unsure if the bucket exists or not, you can use the
        ``S3Connection.lookup`` method, which will either return a valid bucket
        or ``None``.

        If ``validate=False`` is passed, no request is made to the service (no
        charge/communication delay). This is only safe to do if you are **sure**
        the bucket exists.

        If the default ``validate=True`` is passed, a request is made to the
        service to ensure the bucket exists. Prior to Mssapi v2.25.0, this fetched
        a list of keys (but with a max limit set to ``0``, always returning an empty
        list) in the bucket (& included better error messages), at an
        increased expense. As of Mssapi v2.25.0, this now performs a HEAD request
        (less expensive but worse error messages).

        If you were relying on parsing the error message before, you should call
        something like::

            bucket = conn.get_bucket('<bucket_name>', validate=False)
            bucket.get_all_keys(maxkeys=0)

        :type bucket_name: string
        :param bucket_name: The name of the bucket

        :type headers: dict
        :param headers: Additional headers to pass along with the request to
            AWS.

        :type validate: boolean
        :param validate: If ``True``, it will try to verify the bucket exists
            on the service-side. (Default: ``True``)
        """
        if validate:
            return self.head_bucket(bucket_name, headers=headers)
        else:
            return self.bucket_class(self, bucket_name)

    def head_bucket(self, bucket_name, headers=None):
        """
        Determines if a bucket exists by name.

        If the bucket does not exist, an ``S3ResponseError`` will be raised.

        :type bucket_name: string
        :param bucket_name: The name of the bucket

        :type headers: dict
        :param headers: Additional headers to pass along with the request to
            AWS.

        :returns: A <Bucket> object
        """
        response = self.make_request('HEAD', bucket_name, headers=headers)
        body = response.read()
        if response.status == 200:
            return self.bucket_class(self, bucket_name)
        elif response.status == 403:
            # For backward-compatibility, we'll populate part of the exception
            # with the most-common default.
            err = self.provider.storage_response_error(
                response.status,
                response.reason,
                body
            )
            err.error_code = 'AccessDenied'
            err.error_message = 'Access Denied'
            raise err
        elif response.status == 404:
            # For backward-compatibility, we'll populate part of the exception
            # with the most-common default.
            err = self.provider.storage_response_error(
                response.status,
                response.reason,
                body
            )
            err.error_code = 'NoSuchBucket'
            err.error_message = 'The specified bucket does not exist'
            raise err
        else:
            raise self.provider.storage_response_error(
                response.status, response.reason, body)

    def lookup(self, bucket_name, validate=True, headers=None):
        """
        Attempts to get a bucket from S3.

        Works identically to ``S3Connection.get_bucket``, save for that it
        will return ``None`` if the bucket does not exist instead of throwing
        an exception.

        :type bucket_name: string
        :param bucket_name: The name of the bucket

        :type headers: dict
        :param headers: Additional headers to pass along with the request to
            AWS.

        :type validate: boolean
        :param validate: If ``True``, it will try to fetch all keys within the
            given bucket. (Default: ``True``)
        """
        try:
            bucket = self.get_bucket(bucket_name, validate, headers=headers)
        except:
            bucket = None
        return bucket

    def create_bucket(self, bucket_name, headers=None):
        """
        Creates a new located bucket. By default it's in the USA. You can pass
        Location.EU to create a European bucket (S3) or European Union bucket
        (GCS).

        :type bucket_name: string
        :param bucket_name: The name of the new bucket

        :type headers: dict
        :param headers: Additional headers to pass along with the request to AWS.

        :type location: str
        :param location: The location of the new bucket.  You can use one of the
            constants in :class:`mssapi.s3.connection.Location` (e.g. Location.EU,
            Location.USWest, etc.).

        :type policy: :class:`mssapi.s3.acl.CannedACLStrings`
        :param policy: A canned ACL policy that will be applied to the
            new key in S3.

        """

        location=Location.DEFAULT
        policy=None

        check_lowercase_bucketname(bucket_name)

        if policy:
            if headers:
                headers[self.provider.acl_header] = policy
            else:
                headers = {self.provider.acl_header: policy}
        if location == Location.DEFAULT:
            data = ''
        else:
            data = '<CreateBucketConfiguration><LocationConstraint>' + \
                    location + '</LocationConstraint></CreateBucketConfiguration>'
        response = self.make_request('PUT', bucket_name, headers=headers,
                data=data)
        body = response.read()
        if response.status == 409:
            raise self.provider.storage_create_error(
                response.status, response.reason, body)
        if response.status == 200:
            return self.bucket_class(self, bucket_name)
        else:
            raise self.provider.storage_response_error(
                response.status, response.reason, body)

    def delete_bucket(self, bucket, headers=None):
        """
        Removes an S3 bucket.

        In order to remove the bucket, it must first be empty. If the bucket is
        not empty, an ``S3ResponseError`` will be raised.

        :type bucket_name: string
        :param bucket_name: The name of the bucket

        :type headers: dict
        :param headers: Additional headers to pass along with the request to
            AWS.
        """
        response = self.make_request('DELETE', bucket, headers=headers)
        body = response.read()
        if response.status != 204:
            raise self.provider.storage_response_error(
                response.status, response.reason, body)

    def make_request(self, method, bucket='', key='', headers=None, data='',
                     query_args=None, sender=None, override_num_retries=None,
                     retry_handler=None):
        if isinstance(bucket, self.bucket_class):
            bucket = bucket.name
        if isinstance(key, Key):
            key = key.name
        path = self.calling_format.build_path_base(bucket, key)
        mssapi.log.debug('path=%s' % path)
        auth_path = self.calling_format.build_auth_path(bucket, key)
        mssapi.log.debug('auth_path=%s' % auth_path)
        host = self.calling_format.build_host(self.server_name(), bucket)
        if query_args:
            path += '?' + query_args
            mssapi.log.debug('path=%s' % path)
            auth_path += '?' + query_args
            mssapi.log.debug('auth_path=%s' % auth_path)

        return super(S3Connection, self).make_request(
            method, path, headers,
            data, host, auth_path, sender,
            override_num_retries=override_num_retries,
            retry_handler=retry_handler
        )

    '''
    def generate_url_sigv4(self, expires_in, method, bucket='', key='',
                            headers=None, force_http=False,
                            response_headers=None, version_id=None,
                            iso_date=None):

        raise NotSupportError('url_sigv4 not support')

        path = self.calling_format.build_path_base(bucket, key)
        auth_path = self.calling_format.build_auth_path(bucket, key)
        host = self.calling_format.build_host(self.server_name(), bucket)

        # For presigned URLs we should ignore the port if it's HTTPS
        if host.endswith(':443'):
            host = host[:-4]

        params = {}
        if version_id is not None:
            params['VersionId'] = version_id

        http_request = self.build_base_http_request(method, path, auth_path,
                                                    headers=headers, host=host,
                                                    params=params)

        return self._auth_handler.presign(http_request, expires_in,
                                          iso_date=iso_date)


    '''