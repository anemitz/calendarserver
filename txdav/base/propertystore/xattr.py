# -*- test-case-name: txdav.base.propertystore.test.test_xattr,txdav.caldav.datastore,txdav.carddav.datastore -*-
##
# Copyright (c) 2010 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
Property store using filesystem extended attributes.
"""

from __future__ import absolute_import

__all__ = [
    "PropertyStore",
]

import sys
import errno
import urllib
from zlib import compress, decompress, error as ZlibError
from cPickle import UnpicklingError, loads as unpickle
from xattr import xattr

from twext.web2.dav.davxml import WebDAVDocument

from txdav.base.propertystore.base import AbstractPropertyStore, PropertyName, validKey
from txdav.idav import PropertyStoreError


#
# RFC 2518 Section 12.13.1 says that removal of non-existing property
# is not an error.  python-xattr on Linux fails with ENODATA in this
# case.  On OS X, the xattr library fails with ENOATTR, which CPython
# does not expose.  Its value is 93.
#
if sys.platform is "darwin":
    if hasattr(errno, "ENOATTR"):
        _ERRNO_NO_ATTR = errno.ENOATTR
    else:
        _ERRNO_NO_ATTR = 93
else:
    _ERRNO_NO_ATTR = errno.ENODATA


class PropertyStore(AbstractPropertyStore):
    """
    Property store using filesystem extended attributes.

    This implementation uses Bob Ippolito's xattr package, available from::

        http://undefined.org/python/#xattr
    """
    #
    # Dead properties are stored as extended attributes on disk.  In order to
    # avoid conflicts with other attributes, prefix dead property names.
    #
    deadPropertyXattrPrefix = "WebDAV:"

    # Linux seems to require that attribute names use a "user." prefix.
    if sys.platform == "linux2":
        deadPropertyXattrPrefix = "user."

    # There is a 127 character limit for xattr keys so we need to compress/expand
    # overly long namespaces to help stay under that limit now that GUIDs are also
    # encoded in the keys.
    _namespaceCompress = {
        "urn:ietf:params:xml:ns:caldav"                       :"CALDAV:",
        "urn:ietf:params:xml:ns:carddav"                      :"CARDDAV:",
        "http://calendarserver.org/ns/"                       :"CS:",
        "http://cal.me.com/_namespace/"                       :"ME:",
        "http://twistedmatrix.com/xml_namespace/dav/"         :"TD:",
        "http://twistedmatrix.com/xml_namespace/dav/private/" :"TDP:",
    }
    _namespaceExpand = dict([ (v, k) for k, v in _namespaceCompress.iteritems() ])

    def __init__(self, defaultuser, pathFactory):
        """
        Initialize a L{PropertyStore}.

        @param pathFactory: a 0-arg callable that returns the L{CachingFilePath} to set extended attributes on.
        """
        super(PropertyStore, self).__init__(defaultuser)

        self._pathFactory = pathFactory
        # self.attrs = xattr(path.path)
        self.removed = set()
        self.modified = {}


    @property
    def path(self):
        return self._pathFactory()

    @property
    def attrs(self):
        return xattr(self.path.path)

    def __str__(self):
        return "<%s %s>" % (self.__class__.__name__, self.path.path)

    def _encodeKey(self, effective, compressNamespace=True):

        qname, uid = effective
        namespace = self._namespaceCompress.get(qname.namespace, qname.namespace) if compressNamespace else qname.namespace
        result = urllib.quote("{%s}%s" % (namespace, qname.name), safe="{}:")
        if uid:
            result = uid + result
        r = self.deadPropertyXattrPrefix + result
        return r

    def _decodeKey(self, name):

        name = urllib.unquote(name[len(self.deadPropertyXattrPrefix):])

        index1 = name.find("{")
        index2 = name.find("}")

        if (index1 is - 1 or index2 is - 1 or not len(name) > index2):
            raise ValueError("Invalid encoded name: %r" % (name,))
        if index1 == 0:
            uid = None
        else:
            uid = name[:index1]
        propnamespace = name[index1 + 1:index2]
        propnamespace = self._namespaceExpand.get(propnamespace, propnamespace)
        propname = name[index2 + 1:]

        return PropertyName(propnamespace, propname), uid

    #
    # Required implementations
    #

    def _getitem_uid(self, key, uid):
        validKey(key)
        effectiveKey = (key, uid)

        if effectiveKey in self.modified:
            return self.modified[effectiveKey]

        if effectiveKey in self.removed:
            raise KeyError(key)

        try:
            try:
                data = self.attrs[self._encodeKey(effectiveKey)]
            except IOError, e:
                if e.errno in [_ERRNO_NO_ATTR, errno.ENOENT]:
                    raise KeyError(key)
                raise PropertyStoreError(e)
        except KeyError:
            # Check for uncompressed namespace
            if  effectiveKey[0].namespace in self._namespaceCompress:
                try:
                    data = self.attrs[self._encodeKey(effectiveKey, compressNamespace=False)]
                except IOError, e:
                    raise KeyError(key)

                try:
                    # Write it back using the compressed format
                    self.attrs[self._encodeKey(effectiveKey)] = data
                    del self.attrs[self._encodeKey(effectiveKey, compressNamespace=False)]
                except IOError, e:
                    msg = "Unable to upgrade property to compressed namespace: %s" % (
                        key.toString()
                    )
                    self.log_error(msg)
                    raise PropertyStoreError(msg)
            else:
                raise

        #
        # Unserialize XML data from an xattr.  The storage format has changed
        # over time:
        #
        #  1- Started with XML
        #  2- Started compressing the XML due to limits on xattr size
        #  3- Switched to pickle which is faster, still compressing
        #  4- Back to compressed XML for interoperability, size
        #
        # We only write the current format, but we also read the old
        # ones for compatibility.
        #
        legacy = False

        try:
            data = decompress(data)
        except ZlibError:
            legacy = True

        try:
            doc = WebDAVDocument.fromString(data)
        except ValueError:
            try:
                doc = unpickle(data)
            except UnpicklingError:
                msg = "Invalid property value stored on server: %s %s" % (
                    key.toString(), data
                )
                self.log_error(msg)
                raise PropertyStoreError(msg)
            else:
                legacy = True

        if legacy:
            # XXX untested: CDT catches this though.
            self._setitem_uid(key, doc.root_element, uid)

        return doc.root_element

    def _setitem_uid(self, key, value, uid):
        validKey(key)
        effectiveKey = (key, uid)

        if effectiveKey in self.removed:
            self.removed.remove(effectiveKey)
        self.modified[effectiveKey] = value

    def _delitem_uid(self, key, uid):
        validKey(key)
        effectiveKey = (key, uid)

        if effectiveKey in self.modified:
            del self.modified[effectiveKey]
        elif self._encodeKey(effectiveKey) not in self.attrs:
            raise KeyError(key)

        self.removed.add(effectiveKey)

    def _keys_uid(self, uid):
        seen = set()

        try:
            iterattr = iter(self.attrs)
        except IOError, e:
            if e.errno != errno.ENOENT:
                raise
            iterattr = iter(())

        for key in iterattr:
            effectivekey = self._decodeKey(key)
            if effectivekey[1] == uid and effectivekey not in self.removed:
                seen.add(effectivekey)
                yield effectivekey[0]

        for effectivekey in self.modified:
            if effectivekey[1] == uid and effectivekey not in seen:
                yield effectivekey[0]

    #
    # I/O
    #

    def flush(self):
        # FIXME: The transaction may have deleted the file, and then obviously
        # flushing would fail.  Let's try to detect that scenario.  The
        # transaction should not attempt to flush properties if it is also
        # deleting the resource, though, and there are other reasons we might
        # want to know about that the file doesn't exist, so this should be
        # fixed.
        self.path.changed()
        if not self.path.exists():
            return

        attrs = self.attrs
        removed = self.removed
        modified = self.modified

        for key in removed:
            assert key not in modified
            try:
                del attrs[self._encodeKey(key)]
            except KeyError:
                pass
            except IOError, e:
                if e.errno != _ERRNO_NO_ATTR:
                    raise

        for key in modified:
            assert key not in removed
            value = modified[key]
            attrs[self._encodeKey(key)] = compress(value.toxml())

        self.removed.clear()
        self.modified.clear()

    def abort(self):
        self.removed.clear()
        self.modified.clear()