"""Unit tests for protocol.py."""
import copy
from unittest import skip
from unittest.mock import patch

from arroba.tests.testutil import dns_answer
from flask import g
from google.cloud import ndb
from granary import as2
from oauth_dropins.webutil.flask_util import NoContent
from oauth_dropins.webutil.testutil import requests_response
import requests

# import first so that Fake is defined before URL routes are registered
from .testutil import Fake, OtherFake, TestCase

from activitypub import ActivityPub
from app import app
from atproto import ATProto
from models import Follower, Object, PROTOCOLS, Target, User
import protocol
from protocol import Protocol
from ui import UIProtocol
from web import Web
from werkzeug.exceptions import BadRequest

from .test_activitypub import ACTOR
from .test_atproto import DID_DOC
from .test_web import ACTOR_HTML


class ProtocolTest(TestCase):

    def setUp(self):
        super().setUp()
        self.user = self.make_user('foo.com', has_hcard=True)
        g.user = None

    def tearDown(self):
        PROTOCOLS.pop('greedy', None)
        super().tearDown()

    def test_protocols_global(self):
        self.assertEqual(Fake, PROTOCOLS['fake'])
        self.assertEqual(Web, PROTOCOLS['web'])
        self.assertEqual(Web, PROTOCOLS['webmention'])

    def test_for_bridgy_subdomain_for_request(self):
        for domain, expected in [
                ('fake.brid.gy', Fake),
                ('ap.brid.gy', ActivityPub),
                ('activitypub.brid.gy', ActivityPub),
                ('web.brid.gy', Web),
                (None, None),
                ('', None),
                ('brid.gy', None),
                ('www.brid.gy', None),
                ('fed.brid.gy', None),
                ('fake.fed.brid.gy', None),
                ('fake', None),
                ('fake.com', None),
        ]:
            with self.subTest(domain=domain, expected=expected):
                self.assertEqual(expected, Protocol.for_bridgy_subdomain(domain))
                with app.test_request_context('/foo', base_url=f'https://{domain}/'):
                    self.assertEqual(expected, Protocol.for_request())

    def test_for_bridgy_subdomain_for_request_fed(self):
        for url, expected in [
            ('https://fed.brid.gy/', Fake),
            ('http://localhost/foo', Fake),
            ('https://ap.brid.gy/bar', ActivityPub),
            ('https://baz/biff', None),
        ]:
            with self.subTest(url=url, expected=expected):
                self.assertEqual(expected,
                                 Protocol.for_bridgy_subdomain(url, fed=Fake))
                self.assertEqual(expected,
                                 Protocol.for_bridgy_subdomain(url, fed='fake'))
                with app.test_request_context('/foo', base_url=url):
                    self.assertEqual(expected, Protocol.for_request(fed=Fake))

    def test_subdomain_url(self):
        self.assertEqual('https://fa.brid.gy/', Fake.subdomain_url())
        self.assertEqual('https://fa.brid.gy/foo?bar', Fake.subdomain_url('foo?bar'))
        self.assertEqual('https://fed.brid.gy/', UIProtocol.subdomain_url())

    def test_for_id(self):
        for id, expected in [
                (None, None),
                ('', None),
                ('foo://bar', None),
                ('fake:foo', Fake),
                ('at://foo', ATProto),
                ('https://ap.brid.gy/foo/bar', ActivityPub),
                ('https://web.brid.gy/foo/bar', Web),
        ]:
            self.assertEqual(expected, Protocol.for_id(id))

    def test_for_id_true_overrides_none(self):
        class Greedy(Protocol, User):
            @classmethod
            def owns_id(cls, id):
                return True

        self.assertEqual(Greedy, Protocol.for_id('http://foo'))
        self.assertEqual(Greedy, Protocol.for_id('https://bar/baz'))

    def test_for_id_object(self):
        self.store_object(id='http://ui/obj', source_protocol='ui')
        self.assertEqual(UIProtocol, Protocol.for_id('http://ui/obj'))

    def test_for_id_object_missing_source_protocol(self):
        self.store_object(id='http://bad/obj')
        self.assertIsNone(Protocol.for_id('http://bad/obj'))

    @patch('requests.get')
    def test_for_id_activitypub_fetch(self, mock_get):
        mock_get.return_value = self.as2_resp(ACTOR)
        self.assertEqual(ActivityPub, Protocol.for_id('http://ap/actor'))
        self.assertIn(self.as2_req('http://ap/actor'), mock_get.mock_calls)

    @patch('requests.get')
    def test_for_id_activitypub_fetch_fails(self, mock_get):
        mock_get.return_value = requests_response('', status=403)
        self.assertIsNone(Protocol.for_id('http://ap/actor'))
        self.assertIn(self.as2_req('http://ap/actor'), mock_get.mock_calls)
        mock_get.assert_called_once()

    @patch('requests.get')
    def test_for_id_web_fetch(self, mock_get):
        mock_get.return_value = requests_response(ACTOR_HTML)
        self.assertEqual(Web, Protocol.for_id('http://web.site/'))
        self.assertIn(self.req('http://web.site/'), mock_get.mock_calls)

    @patch('requests.get')
    def test_for_id_web_fetch_no_mf2(self, mock_get):
        mock_get.return_value = requests_response('<html></html>')
        self.assertIsNone(Protocol.for_id('http://web.site/'))
        self.assertIn(self.req('http://web.site/'), mock_get.mock_calls)

    def test_for_handle_deterministic(self):
        for handle, expected in [
            (None, (None, None)),
            ('', (None, None)),
            ('foo://bar', (None, None)),
            ('fake:foo', (None, None)),
            ('fake:handle:foo', (Fake, None)),
            ('@me@foo', (ActivityPub, None)),
        ]:
            self.assertEqual(expected, Protocol.for_handle(handle))

    def test_for_handle_stored_user(self):
        user = self.make_user(id='user.com', cls=Web)
        self.assertEqual('user.com', user.handle)
        self.assertEqual((Web, 'user.com'), Protocol.for_handle('user.com'))

    @patch('dns.resolver.resolve', return_value = dns_answer(
            '_atproto.han.dull.', '"did=did:plc:123abc"'))
    def test_for_handle_atproto_resolve(self, _):
        self.assertEqual((ATProto, 'did:plc:123abc'), Protocol.for_handle('han.dull'))

    def test_load(self):
        Fake.fetchable['foo'] = {'x': 'y'}

        loaded = Fake.load('foo')
        self.assert_equals({'x': 'y'}, loaded.our_as1)
        self.assertFalse(loaded.changed)
        self.assertTrue(loaded.new)

        self.assertIsNotNone(Object.get_by_id('foo'))
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_existing(self):
        self.store_object(id='foo', our_as1={'x': 'y'})

        loaded = Fake.load('foo')
        self.assert_equals({'x': 'y'}, loaded.our_as1)
        self.assertFalse(loaded.changed)
        self.assertFalse(loaded.new)

        self.assertEqual([], Fake.fetched)

    def test_load_existing_empty_deleted(self):
        stored = self.store_object(id='foo', deleted=True)

        loaded = Fake.load('foo')
        self.assert_entities_equal(stored, loaded)
        self.assertFalse(loaded.changed)
        self.assertFalse(loaded.new)

        self.assertEqual([], Fake.fetched)

    def test_load_cached(self):
        obj = Object(id='foo', our_as1={'x': 'y'})
        protocol.objects_cache['foo'] = obj
        loaded = Fake.load('foo')
        self.assert_entities_equal(obj, loaded)

        # check that it's a separate copy of the entity in the cache
        # https://github.com/snarfed/bridgy-fed/issues/558#issuecomment-1603203927
        loaded.our_as1 = {'a': 'b'}
        self.assertEqual({'x': 'y'}, Protocol.load('foo').our_as1)

    def test_load_remote_true_existing_empty(self):
        Fake.fetchable['foo'] = {'x': 'y'}
        Object(id='foo').put()

        loaded = Fake.load('foo', remote=True)
        self.assertEqual({'id': 'foo', 'x': 'y'}, loaded.as1)
        self.assertTrue(loaded.changed)
        self.assertFalse(loaded.new)
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_remote_true_new_empty(self):
        Fake.fetchable['foo'] = None
        self.store_object(id='foo', our_as1={'x': 'y'})

        loaded = Fake.load('foo', remote=True)
        self.assertIsNone(loaded.as1)
        self.assertTrue(loaded.changed)
        self.assertFalse(loaded.new)
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_remote_true_unchanged(self):
        obj = self.store_object(id='foo', our_as1={'x': 'stored'},
                                source_protocol='fake')
        Fake.fetchable['foo'] = {'x': 'stored'}

        loaded = Fake.load('foo', remote=True)
        self.assert_entities_equal(obj, loaded,
                                   ignore=['expire', 'created', 'updated'])
        self.assertFalse(loaded.changed)
        self.assertFalse(loaded.new)
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_remote_true_local_false(self):
        Fake.fetchable['foo'] = our_as1={'x': 'y'}

        loaded = Fake.load('foo', local=False, remote=True)
        self.assertEqual({'id': 'foo', 'x': 'y'}, loaded.as1)
        self.assertIsNone(loaded.changed)
        self.assertIsNone(loaded.new)
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_remote_true_changed(self):
        self.store_object(id='foo', our_as1={'content': 'stored'})
        Fake.fetchable['foo'] = {'content': 'new'}

        loaded = Fake.load('foo', remote=True)
        self.assert_equals({'content': 'new'}, loaded.our_as1)
        self.assertTrue(loaded.changed)
        self.assertFalse(loaded.new)
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_remote_false(self):
        self.assertIsNone(Fake.load('nope', remote=False))
        self.assertEqual([], Fake.fetched)

        obj = self.store_object(id='foo', our_as1={'content': 'stored'})
        self.assert_entities_equal(obj, Fake.load('foo', remote=False))
        self.assertEqual([], Fake.fetched)

    def test_load_remote_false_existing_object_empty(self):
        obj = self.store_object(id='foo')
        self.assert_entities_equal(obj, Protocol.load('foo', remote=False))

    def test_load_local_false_missing(self):
        self.assertIsNone(Fake.load('foo', local=False))
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_local_false_existing(self):
        self.store_object(id='foo', our_as1={'content': 'stored'}, source_protocol='ui')

        Fake.fetchable['foo'] = {'foo': 'bar'}
        Fake.load('foo', local=False)
        self.assert_object('foo', source_protocol='fake', our_as1={'foo': 'bar'})
        self.assertEqual(['foo'], Fake.fetched)

    def test_load_remote_false_local_false_assert(self):
        with self.assertRaises(AssertionError):
            Fake.load('nope', local=False, remote=False)

    def test_owner_key(self):
        user = Fake(id='fake:a')
        a_key = user.key

        for expected, obj in [
                (None, Object()),
                (None, Object(our_as1={})),
                (None, Object(our_as1={'foo': 'bar'})),
                (None, Object(our_as1={'foo': 'bar'})),
                (None, Object(our_as1={'actor': ''})),
                (a_key, Object(our_as1={'actor': 'fake:a'})),
                (a_key, Object(our_as1={'author': 'fake:a'})),
        ]:
            self.assertEqual(expected, Fake.actor_key(obj))

        g.user = user
        self.assertEqual(a_key, Fake.actor_key(Object()))
        self.assertIsNone(Fake.actor_key(Object(), default_g_user=False))

    def test_targets_checks_blocklisted_per_protocol(self):
        """_targets should call the target protocol's is_blocklisted()."""
        # non-ATProto account, ATProto target (PDS) is http://localhost,
        # shouldn't be blocklisted
        user = self.make_user(id='fake:user', cls=Fake, atproto_did='did:plc:foo')
        did_doc = copy.deepcopy(DID_DOC)
        did_doc['service'][0]['serviceEndpoint'] = 'http://localhost/'
        self.store_object(id='did:plc:foo', raw=did_doc)

        # store Objects so we don't try to fetch them remotely
        self.store_object(id='at://did:plc:foo/co.ll/post', our_as1={'foo': 'bar'})
        self.store_object(id='fake:post', our_as1={'foo': 'baz'})

        obj = Object(our_as1={
            'id': 'other:reply',
            'objectType': 'note',
            'inReplyTo': [
                'fake:post',
                'fake:blocklisted-post',
                'https://t.co/foo',
                'http://localhost/post',
                'at://did:plc:foo/co.ll/post',
            ],
        })
        self.assertCountEqual([
            Target(protocol='fake', uri='fake:post:target'),
            Target(protocol='atproto', uri='http://localhost/'),
        ], Protocol.targets(obj).keys())

    @patch('requests.get', return_value=requests_response({}))
    def test_targets_converts_copies_to_originals(self, mock_get):
        """targets should convert User/Object.copies to their originals."""
        alice = self.make_user('fake:alice', cls=Fake,
                               copies=[Target(uri='did:plc:alice', protocol='atproto')])
        bob = self.make_user(
            'fake:bob', cls=OtherFake,
            copies=[Target(uri='other:bob', protocol='other')])
        obj = self.store_object(
            id='fake:post', our_as1={'foo': 9},
            copies=[Target(uri='at://did:plc:eve/post/789', protocol='fake')])

        Fake.fetchable = {
            'fake:alice': {'foo': 1},
            'fake:bob': {'foo': 2},
        }
        OtherFake.fetchable = {
            'other:bob': {'foo': 3},
        }

        obj = Object(our_as1={
            'id': 'other:reply',
            'objectType': 'note',
            'inReplyTo': [
                'at://did:web:unknown/post/123',
                'at://did:plc:eve/post/789',
            ],
            'tags': [{
                'objectType': 'mention',
                'url': 'did:plc:alice',
            }, {
                'objectType': 'mention',
                'url': 'other:bob',
            }],
        })
        self.assertCountEqual([
            Target(uri='fake:post:target', protocol='fake'),
            Target(uri='fake:alice:target', protocol='fake'),
            Target(uri='fake:bob:target', protocol='fake'),
            Target(uri='other:bob:target', protocol='otherfake'),
        ], Protocol.targets(obj).keys())


class ProtocolReceiveTest(TestCase):

    def setUp(self):
        super().setUp()
        g.user = self.user = self.make_user('fake:user', cls=Fake, obj_id='fake:user')
        self.alice = self.make_user('fake:alice', cls=Fake, obj_id='fake:alice')
        self.bob = self.make_user('fake:bob', cls=Fake, obj_id='fake:bob')

    def assert_object(self, id, **props):
        props.setdefault('source_protocol', 'fake')
        props.setdefault('delivered_protocol', 'fake')
        return super().assert_object(id, **props)

    def make_followers(self):
        Follower.get_or_create(to=self.user, from_=self.alice)
        Follower.get_or_create(to=self.user, from_=self.bob)
        Follower.get_or_create(to=self.user, from_=Fake(id='fake:eve'),
                               status='inactive')

    def test_create_post(self):
        self.make_followers()

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
        }
        create_as1 = {
            'id': 'fake:create',
            'objectType': 'activity',
            'verb': 'post',
            'actor': 'fake:user',
            'object': post_as1,
        }
        self.assertEqual('OK', Fake.receive_as1(create_as1))

        self.assert_object('fake:post',
                           our_as1=post_as1,
                           type='note',
                           feed=[self.alice.key, self.bob.key],
                           )
        obj = self.assert_object('fake:create',
                                 status='complete',
                                 our_as1=create_as1,
                                 delivered=['shared:target'],
                                 type='post',
                                 users=[g.user.key],
                                 notify=[],
                                 )

        self.assertEqual([(obj, 'shared:target')], Fake.sent)

    def test_create_post_bare_object(self):
        self.make_followers()

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
            'author': 'fake:user',
        }
        self.assertEqual('OK', Fake.receive_as1(post_as1))

        self.assert_object('fake:post',
                           our_as1=post_as1,
                           type='note',
                           feed=[self.alice.key, self.bob.key],
                           )

        obj = self.assert_object('fake:post#bridgy-fed-create',
                                 status='complete',
                                 our_as1={
                                     'objectType': 'activity',
                                     'verb': 'post',
                                     'id': 'fake:post#bridgy-fed-create',
                                     'actor': 'fake:user',
                                     'object': post_as1,
                                     'published': '2022-01-02T03:04:05+00:00',
                                 },
                                 delivered=['shared:target'],
                                 type='post',
                                 users=[g.user.key],
                                 notify=[],
                                 )

        self.assertEqual([(obj, 'shared:target')], Fake.sent)

    def test_create_post_bare_object_existing_failed_create(self):
        self.make_followers()

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
            'author': 'fake:user',
        }
        self.store_object(id='fake:post', our_as1=post_as1)
        self.store_object(id='fake:post#bridgy-fed-create', status='failed')

        self.assertEqual('OK', Fake.receive_as1(post_as1))

        obj = self.assert_object('fake:post#bridgy-fed-create',
                                 status='complete',
                                 delivered=['shared:target'],
                                 type='post',
                                 users=[g.user.key],
                                 ignore=['our_as1'],
                                 )

        self.assertEqual([(obj, 'shared:target')], Fake.sent)

    def test_create_post_bare_object_no_existing_create(self):
        self.make_followers()

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
            'author': 'fake:user',
        }
        self.store_object(id='fake:post', our_as1=post_as1)

        self.assertEqual('OK', Fake.receive_as1(post_as1))

        obj = self.assert_object('fake:post#bridgy-fed-create',
                                 status='complete',
                                 delivered=['shared:target'],
                                 type='post',
                                 users=[g.user.key],
                                 ignore=['our_as1'],
                                 )

        self.assertEqual([(obj, 'shared:target')], Fake.sent)

    def test_update_post(self):
        self.make_followers()

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
        }
        self.store_object(id='fake:post', our_as1=post_as1)

        update_as1 = {
            'id': 'fake:update',
            'objectType': 'activity',
            'verb': 'update',
            'actor': 'fake:user',
            'object': post_as1,
        }
        self.assertEqual('OK', Fake.receive_as1(update_as1))

        self.assert_object('fake:post',
                           our_as1=post_as1,
                           type='note',
                           feed=[self.alice.key, self.bob.key],
                           )
        obj = self.assert_object('fake:update',
                                 status='complete',
                                 our_as1=update_as1,
                                 delivered=['shared:target'],
                                 type='update',
                                 users=[g.user.key],
                                 notify=[],
                                 )

        self.assertEqual([(obj, 'shared:target')], Fake.sent)

    def test_update_post_bare_object(self):
        self.make_followers()

        # post has no author
        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
            'content': 'first',
        }
        self.store_object(id='fake:post', our_as1=post_as1)
        existing = Object.get_by_id('fake:post')

        post_as1['content'] = 'second'
        with self.assertRaises(NoContent):
            Fake.receive_as1(post_as1)

        post_as1['updated'] = '2022-01-02T03:04:05+00:00'
        self.assert_object('fake:post',
                           our_as1=post_as1,
                           type='note',
                           feed=[],
                           )

        update_id = 'fake:post#bridgy-fed-update-2022-01-02T03:04:05+00:00'
        obj = self.assert_object(update_id,
                                 status='ignored',
                                 our_as1={
                                     'objectType': 'activity',
                                     'verb': 'update',
                                     'id': update_id,
                                     'object': post_as1,
                                 },
                                 delivered=[],
                                 type='update',
                                 # post has no author
                                 users=[],
                                 notify=[],
                                 )

        self.assertEqual([], Fake.sent)

    def test_create_reply(self):
        self.make_followers()

        Fake.fetchable['fake:post'] = {
            'objectType': 'note',
            'author': 'fake:bob',
        }
        reply_as1 = {
            'id': 'fake:reply',
            'objectType': 'note',
            'inReplyTo': 'fake:post',
            'author': 'fake:alice',
        }
        create_as1 = {
            'id': 'fake:create',
            'objectType': 'activity',
            'verb': 'post',
            'actor': 'fake:user',
            'object': reply_as1,
        }
        self.assertEqual('OK', Fake.receive_as1(create_as1))

        self.assert_object('fake:reply',
                           our_as1=reply_as1,
                           type='note',
                           )
        obj = self.assert_object('fake:create',
                                 status='complete',
                                 our_as1=create_as1,
                                 delivered=['fake:post:target'],
                                 type='post',
                                 users=[g.user.key, self.alice.key],
                                 notify=[self.bob.key],
                                 )

        self.assertEqual([(obj, 'fake:post:target')], Fake.sent)

    def test_create_reply_bare_object(self):
        self.make_followers()

        reply_as1 = {
            'id': 'fake:reply',
            'objectType': 'note',
            'inReplyTo': 'fake:post',
            'author': 'fake:alice',
        }
        Fake.fetchable['fake:post'] = {
            'objectType': 'note',
            'id': 'fake:post',
            'author': 'fake:bob',
        }
        self.assertEqual('OK', Fake.receive_as1(reply_as1))

        self.assert_object('fake:reply',
                           our_as1=reply_as1,
                           type='note',
                           )

        create_as1 = {
            'id': 'fake:reply#bridgy-fed-create',
            'objectType': 'activity',
            'verb': 'post',
            'actor': 'fake:alice',
            'object': reply_as1,
            'published': '2022-01-02T03:04:05+00:00',
        }
        obj = self.assert_object('fake:reply#bridgy-fed-create',
                                 status='complete',
                                 our_as1=create_as1,
                                 delivered=['fake:post:target'],
                                 type='post',
                                 users=[self.alice.key],
                                 notify=[self.bob.key],
                                 )

        self.assertEqual([(obj, 'fake:post:target')], Fake.sent)

    def test_update_reply(self):
        self.make_followers()

        Fake.fetchable['fake:post'] = {
            'objectType': 'note',
            'author': 'fake:bob',
        }
        reply_as1 = {
            'id': 'fake:reply',
            'objectType': 'note',
            'inReplyTo': 'fake:post',
            'author': 'fake:alice',
        }
        self.store_object(id='fake:reply', our_as1=reply_as1)

        update_as1 = {
            'id': 'fake:update',
            'objectType': 'activity',
            'verb': 'update',
            'actor': 'fake:user',
            'object': reply_as1,
        }
        self.assertEqual('OK', Fake.receive_as1(update_as1))

        self.assert_object('fake:reply',
                           our_as1=reply_as1,
                           type='note',
                           )
        obj = self.assert_object('fake:update',
                                 status='complete',
                                 our_as1=update_as1,
                                 delivered=['fake:post:target'],
                                 type='update',
                                 users=[g.user.key, self.alice.key],
                                 notify=[self.bob.key],
                                 )
        self.assertEqual([(obj, 'fake:post:target')], Fake.sent)

    def test_repost(self):
        self.make_followers()

        Fake.fetchable['fake:post'] = {
            'objectType': 'note',
            'author': 'fake:bob',
        }
        repost_as1 = {
            'id': 'fake:repost',
            'objectType': 'activity',
            'verb': 'share',
            'actor': 'fake:user',
            'object': 'fake:post',
        }
        self.assertEqual('OK', Fake.receive_as1(repost_as1))

        obj = self.assert_object('fake:repost',
                                 status='complete',
                                 our_as1={
                                     **repost_as1,
                                     'object': {
                                         'id': 'fake:post',
                                         'objectType': 'note',
                                         'author': 'fake:bob',
                                     },
                                 },
                                 delivered=['fake:post:target', 'shared:target'],
                                 type='share',
                                 users=[g.user.key],
                                 notify=[self.bob.key],
                                 feed=[self.alice.key, self.bob.key],
                                 )
        self.assertEqual([
            (obj, 'fake:post:target'),
            (obj, 'shared:target'),
        ], Fake.sent)

    def test_repost_twitter_blocklisted(self):
        self._test_repost_blocklisted_error('https://twitter.com/foo')

    def test_repost_bridgy_fed_blocklisted(self):
        self._test_repost_blocklisted_error('https://fed.brid.gy/foo')

    def _test_repost_blocklisted_error(self, orig_url):
        """Reposts of non-fediverse (ie blocklisted) sites aren't yet supported."""
        repost_as1 = {
            'id': 'fake:repost',
            'objectType': 'activity',
            'verb': 'share',
            'actor': 'fake:user',
            'object': orig_url,
        }
        with self.assertRaises(NoContent):
            Fake.receive_as1(repost_as1)

        obj = self.assert_object('fake:repost',
                                 status='ignored',
                                 our_as1=repost_as1,
                                 delivered=[],
                                 type='share',
                                 users=[g.user.key],
                                 )
        self.assertEqual([], Fake.sent)

    def test_like(self):
        Fake.fetchable['fake:post'] = {
            'objectType': 'note',
            'author': 'fake:bob',
        }

        like_as1 = {
            'id': 'fake:like',
            'objectType': 'activity',
            'verb': 'like',
            'actor': 'fake:user',
            'object': 'fake:post',
        }
        self.assertEqual('OK', Fake.receive_as1(like_as1))

        like_obj = self.assert_object('fake:like',
                                      users=[g.user.key],
                                      notify=[self.bob.key],
                                      status='complete',
                                      our_as1=like_as1,
                                      delivered=['fake:post:target'],
                                      type='like',
                                      object_ids=['fake:post'])

        self.assertEqual([(like_obj, 'fake:post:target')], Fake.sent)

    def test_like_no_object_error(self):
        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'fake:like',
                'objectType': 'activity',
                'verb': 'like',
                'actor': 'fake:user',
                'object': None,
        })

    def test_share_no_object_error(self):
        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'fake:share',
                'objectType': 'activity',
                'verb': 'share',
                'actor': 'fake:user',
                'object': None,
        })

    def test_delete(self):
        g.user = None  # should use activity's actor
        self.make_followers()

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
            'author': 'fake:user',
        }
        self.store_object(id='fake:post', our_as1=post_as1)

        delete_as1 = {
            'id': 'fake:delete',
            'objectType': 'activity',
            'verb': 'delete',
            'actor': 'fake:user',
            'object': 'fake:post',
        }
        self.assertEqual('OK', Fake.receive_as1(delete_as1))

        self.assert_object('fake:post',
                           our_as1=post_as1,
                           deleted=True,
                           source_protocol=None,
                           feed=[self.alice.key, self.bob.key],
                           )

        obj = self.assert_object('fake:delete',
                                 status='complete',
                                 our_as1=delete_as1,
                                 delivered=['shared:target'],
                                 type='delete',
                                 users=[self.user.key],
                                 notify=[],
                                 )
        self.assertEqual([(obj, 'shared:target')], Fake.sent)

    def test_delete_no_followers_no_stored_object(self):
        g.user = None  # should use activity's actor
        delete_as1 = {
            'id': 'fake:delete',
            'objectType': 'activity',
            'verb': 'delete',
            'actor': 'fake:user',
            'object': 'fake:post',
        }
        with self.assertRaises(NoContent):
            self.assertEqual('OK', Fake.receive_as1(delete_as1))

        self.assert_object('fake:post',
                           deleted=True,
                           source_protocol=None,
                           feed=[],
                           )

        self.assert_object('fake:delete',
                           status='ignored',
                           our_as1=delete_as1,
                           delivered=[],
                           type='delete',
                           users=[self.user.key],
                           notify=[],
                           )
        self.assertEqual([], Fake.sent)

    def test_delete_actor(self):
        g.user = None

        follower = Follower.get_or_create(to=self.user, from_=self.alice)
        followee = Follower.get_or_create(to=self.alice, from_=self.bob)
        other = Follower.get_or_create(to=self.user, from_=self.bob)
        self.assertEqual(3, Follower.query().count())

        with self.assertRaises(NoContent):
            Fake.receive_as1({
                'objectType': 'activity',
                'verb': 'delete',
                'id': 'fake:delete',
                'object': 'fake:alice',
            })

        self.assertEqual(3, Follower.query().count())
        self.assertEqual('inactive', follower.key.get().status)
        self.assertEqual('inactive', followee.key.get().status)
        self.assertEqual('active', other.key.get().status)

        self.assert_object('fake:alice',
                           deleted=True,
                           source_protocol=None,
                           )

    @patch.object(Fake, 'send')
    @patch.object(Fake, 'target_for')
    def test_send_error(self, mock_target_for, mock_send):
        """Two targets. First send fails, second succeeds."""
        self.make_followers()

        mock_target_for.side_effect = [
            'target:1',
            'target:2',
        ]

        post_as1 = {
            'id': 'fake:post',
            'objectType': 'note',
        }
        create_as1 = {
            'id': 'fake:create',
            'objectType': 'activity',
            'verb': 'post',
            'actor': 'fake:user',
            'object': post_as1,
        }

        sent = []
        def send(obj, url, log_data=True):
            self.assertEqual(create_as1, obj.as1)
            if not sent:
                self.assertEqual('target:1', url)
                sent.append('fail')
                raise BadRequest()
            else:
                self.assertEqual('target:2', url)
                sent.append('sent')
                return True

        mock_send.side_effect = send

        self.assertEqual('OK', Fake.receive_as1(create_as1))

        self.assert_object('fake:post',
                           our_as1=post_as1,
                           type='note',
                           feed=[self.alice.key, self.bob.key],
                           )
        obj = self.assert_object('fake:create',
                                 status='complete',
                                 our_as1=create_as1,
                                 delivered=['target:2'],
                                 failed=['target:1'],
                                 type='post',
                                 users=[g.user.key],
                                 )

        self.assertEqual(['fail', 'sent'], sent)

    def test_update_profile(self):
        self.make_followers()

        id = 'fake:user#update-2022-01-02T03:04:05+00:00'
        update_as1 = {
            'objectType': 'activity',
            'verb': 'update',
            'id': id,
            'actor': 'fake:user',
            'object': {
                'objectType': 'person',
                'id': 'fake:user',
                'displayName': 'Ms. ☕ Baz',
                'urls': [{'displayName': 'Ms. ☕ Baz', 'value': 'https://user.com/'}],
                'updated': '2022-01-02T03:04:05+00:00',
            },
        }

        Fake.receive_as1(update_as1)

        # profile object
        self.assert_object('fake:user',
                           our_as1=update_as1['object'],
                           type='person',
                           feed=[],
                           )

        # update activity
        update_as1['actor'] = update_as1['object']
        update_obj = self.assert_object(
            id,
            users=[g.user.key],
            status='complete',
            our_as1=update_as1,
            delivered=['shared:target'],
            type='update',
            object_ids=['fake:user'],
        )

        self.assertEqual([(update_obj, 'shared:target')], Fake.sent)

    def test_mention_object(self, *mocks):
        self.alice.obj.our_as1 = {'id': 'fake:alice', 'objectType': 'person'}
        self.alice.obj.put()
        self.bob.obj.our_as1 = {'id': 'fake:bob', 'objectType': 'person'}
        self.bob.obj.put()

        mention_as1 = {
            'objectType': 'note',
            'id': 'fake:mention',
            'author': 'fake:user',
            'content': 'something',
            'tags': [{
                'objectType': 'mention',
                'url': 'fake:alice',
            }, {
                'objectType': 'mention',
                'url': 'fake:bob',
            }],
        }
        self.assertEqual('OK', Fake.receive_as1(mention_as1))

        self.assert_object('fake:mention',
                           our_as1=mention_as1,
                           type='note',
                           )

        obj = self.assert_object('fake:mention#bridgy-fed-create',
                                 status='complete',
                                 our_as1={
                                     'objectType': 'activity',
                                     'verb': 'post',
                                     'id': 'fake:mention#bridgy-fed-create',
                                     'actor': 'fake:user',
                                     'object': mention_as1,
                                     'published': '2022-01-02T03:04:05+00:00',
                                 },
                                 delivered=['fake:alice:target', 'fake:bob:target'],
                                 type='post',
                                 users=[g.user.key],
                                 notify=[self.alice.key, self.bob.key],
                                 )

        self.assertEqual([(obj, 'fake:alice:target'), (obj, 'fake:bob:target')],
                         Fake.sent)

    def test_follow(self):
        self._test_follow()

    def test_follow_no_g_user(self):
        """No user from request, eg delivered to our ActivityPub shared inbox."""
        g.user = None
        self.test_follow()

    def test_follow_existing_inactive(self):
        follower = Follower.get_or_create(to=g.user, from_=self.alice,
                                          status='inactive')
        self._test_follow()

    def test_follow_actor_object_composite_objects(self):
        self._test_follow(actor={'id': 'fake:alice', 'objectType': 'person'},
                          object={'id': 'fake:user', 'objectType': 'person'})

    def _test_follow(self, **extra):
        Fake.fetchable['fake:alice'] = {}

        follow_as1 = {
            'id': 'fake:follow',
            'objectType': 'activity',
            'verb': 'follow',
            'actor': 'fake:alice',
            'object': 'fake:user',
            **extra,
        }
        self.assertEqual('OK', Fake.receive_as1(follow_as1))

        user = Fake.get_by_id('fake:user')
        follow_obj = self.assert_object('fake:follow',
                                        our_as1=follow_as1,
                                        status='complete',
                                        users=[self.alice.key],
                                        notify=[user.key],
                                        feed=[],
                                        delivered=['fake:user:target'],
                                        )

        accept_id = 'https://fa.brid.gy/ap/fake:user/followers#accept-fake:follow'
        accept_as1 = {
            'id': accept_id,
            'objectType': 'activity',
            'verb': 'accept',
            'actor': 'fake:user',
            'object': follow_as1,
        }
        accept_obj = self.assert_object(accept_id,
                                        our_as1=accept_as1,
                                        type='accept',
                                        status='complete',
                                        delivered=['fake:alice:target'],
                                        users=[],
                                        notify=[],
                                        feed=[],
                                        source_protocol=None,
                                        )

        self.assertEqual([
            (accept_obj, 'fake:alice:target'),
            (follow_obj, 'fake:user:target'),
        ], Fake.sent)

        self.assert_entities_equal(
            Follower(to=user.key, from_=self.alice.key, status='active',
                     follow=follow_obj.key),
            Follower.query().fetch(),
            ignore=['created', 'updated'],
        )

    def test_follow_no_actor(self):
        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'fake:follow',
                'objectType': 'activity',
                'verb': 'follow',
                'object': 'fake:user',
            })

        self.assertEqual([], Follower.query().fetch())
        self.assertEqual([], Fake.sent)

    def test_follow_no_object(self):
        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'fake:follow',
                'objectType': 'activity',
                'verb': 'follow',
                'actor': 'fake:alice',
            })

        self.assertEqual([], Follower.query().fetch())
        self.assertEqual([], Fake.sent)

    def test_follow_object_unknown_protocol(self):
        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'fake:follow',
                'objectType': 'activity',
                'verb': 'follow',
                'actor': 'fake:alice',
                'object': 'unknown:bob',
            })

        self.assertEqual([], Follower.query().fetch())
        self.assertEqual([], Fake.sent)

    def test_stop_following(self):
        follower = Follower.get_or_create(to=g.user, from_=self.alice)

        g.user.obj.our_as1 = {'id': 'fake:user'}
        g.user.obj.put()

        stop_as1 = {
            'id': 'fake:stop-following',
            'objectType': 'activity',
            'verb': 'stop-following',
            'actor': 'fake:alice',
            'object': 'fake:user',
        }
        self.assertEqual('OK', Fake.receive_as1(stop_as1))

        stop_obj = self.assert_object('fake:stop-following',
                                      our_as1=stop_as1,
                                      type='stop-following',
                                      status='complete',
                                      delivered=['fake:user:target'],
                                      users=[self.alice.key],
                                      notify=[],
                                      feed=[],
                                      )

        self.assertEqual('inactive', follower.key.get().status)
        self.assertEqual([(stop_obj, 'fake:user:target')], Fake.sent)

    def test_stop_following_doesnt_exist(self):
        g.user.obj.our_as1 = {'id': 'fake:user'}
        g.user.obj.put()

        self.assertEqual('OK', Fake.receive_as1({
            'id': 'fake:stop-following',
            'objectType': 'activity',
            'verb': 'stop-following',
            'actor': 'fake:alice',
            'object': 'fake:user',
        }))

        self.assertEqual(0, Follower.query().count())

        self.assertEqual(1, len(Fake.sent))
        obj, target = Fake.sent[0]
        self.assertEqual('fake:stop-following', obj.key.id())
        self.assertEqual('fake:user:target', target)

    def test_stop_following_inactive(self):
        follower = Follower.get_or_create(to=g.user, from_=self.alice,
                                          status='inactive')
        Fake.fetchable['fake:alice'] = {}
        g.user.obj.our_as1 = {'id': 'fake:user'}
        g.user.obj.put()

        self.assertEqual('OK', Fake.receive_as1({
            'id': 'fake:stop-following',
            'objectType': 'activity',
            'verb': 'stop-following',
            'actor': 'fake:alice',
            'object': 'fake:user',
        }))

        self.assertEqual('inactive', follower.key.get().status)

        self.assertEqual(1, len(Fake.sent))
        obj, target = Fake.sent[0]
        self.assertEqual('fake:stop-following', obj.key.id())
        self.assertEqual('fake:user:target', target)

    @skip
    def test_receive_from_bridgy_fed_domain_fails(self):
        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'https://fed.brid.gy/r/foo',
            })

        self.assertIsNone(Object.get_by_id('https://fed.brid.gy/r/foo'))

        with self.assertRaises(BadRequest):
            Fake.receive_as1({
                'id': 'fake:foo',
                'actor': 'https://ap.brid.gy/user.com',
            })

        self.assertIsNone(Object.get_by_id('foo'))
        self.assertIsNone(Object.get_by_id('https://ap.brid.gy/user.com'))

    def test_skip_same_protocol(self):
        self.make_user('other:carol', cls=OtherFake, obj_id='other:carol')
        self.make_user('other:dan', cls=OtherFake, obj_id='other:dan')

        OtherFake.fetchable = {
            'other:carol': {},
        }

        follow_as1 = {
            'id': 'http://x.com/follow',
            'objectType': 'activity',
            'verb': 'follow',
            'actor': 'other:carol',
            'object': ['other:dan', 'fake:alice'],
        }

        self.assertEqual('OK', OtherFake.receive_as1(follow_as1))

        self.assertEqual(1, len(OtherFake.sent))
        self.assertEqual('accept', OtherFake.sent[0][0].type)

        self.assertEqual(1, len(Fake.sent))
        self.assertEqual('follow', Fake.sent[0][0].type)

        followers = Follower.query().fetch()
        self.assertEqual(1, len(followers))
        self.assertEqual(self.alice.key, followers[0].to)

    def test_skip_same_domain(self):
        Fake.fetchable = {
            'http://x.com/alice': {},
            'http://x.com/bob': {},
            'http://x.com/eve': {},
        }

        follow_as1 = {
            'id': 'http://x.com/follow',
            'objectType': 'activity',
            'verb': 'follow',
            'actor': 'http://x.com/alice',
            'object': ['http://x.com/bob', 'http://x.com/eve'],
        }

        with self.assertRaises(NoContent):
            Fake.receive_as1(follow_as1)

        (bob_obj, bob_target), (eve_obj, eve_target) = Fake.sent
        self.assertEqual('https://fa.brid.gy/ap/http://x.com/bob/followers#accept-http://x.com/follow',
                         bob_obj.key.id())
        self.assertEqual('http://x.com/alice:target', bob_target)
        self.assertEqual('https://fa.brid.gy/ap/http://x.com/eve/followers#accept-http://x.com/follow',
                         eve_obj.key.id())
        self.assertEqual('http://x.com/alice:target', eve_target)

        self.assert_object('http://x.com/follow',
                           our_as1=follow_as1,
                           status='ignored',
                           users=[ndb.Key(Fake, 'http://x.com/alice')],
                           notify=[ndb.Key(Fake, 'http://x.com/bob'),
                                   ndb.Key(Fake, 'http://x.com/eve')],
                           )
        self.assertEqual(2, Follower.query().count())

    def test_task_handler(self):
        note = {
            'id': 'fake:post',
            'objectType': 'note',
            'author': 'fake:other',
        }
        obj = self.store_object(id='fake:post', our_as1=note,
                                source_protocol='fake')

        self.client.post('/_ah/queue/receive', data={'obj': obj.key.urlsafe()})
        obj = Object.get_by_id('fake:post#bridgy-fed-create')
        self.assertEqual('ignored', obj.status)
