"""Customer selection exercises real HTTP and synthetic SQLite sources only."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from inquiry_product.manual_sync import CONFIG_FILE, configure_connection
from inquiry_product.web_server import create_server
from inquiry_product.workspace import Workspace


class CustomerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = Workspace.init(self.root / 'customer', 'customer-test', {
            'id': 'customer-test', 'name': 'Test company', 'mode': 'customer',
            'industry': 'test', 'required_fields': ['quantity'], 'rules': ['Verify before quoting'], 'knowledge': [],
        }, mode='customer')
        self.source = self.root / 'synthetic-bridge.db'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)')
            db.execute('CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, '
                       'timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT, PRIMARY KEY(id,chat_jid))')
        self.jid_a = '447700900001@s.whatsapp.net'
        self.jid_b = '447700900002@s.whatsapp.net'
        self.add_chat(self.jid_a, 'Synthetic Alice', 'a1')
        self.add_chat(self.jid_b, 'Synthetic Bob', 'b1')
        configure_connection(self.ws, self.source, 'sales-test', [{'jid': self.jid_a, 'display_name': 'Synthetic Alice'}])
        def forbidden_runner():
            raise AssertionError('Customer management must never call a model')
        self.server = create_server(self.ws, port=0, runner_factory=forbidden_runner,
                                    model_catalog_provider=lambda: {'source': 'offline', 'models': []})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def add_chat(self, jid, name, identity):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?)', (jid, name, '2026-09-25T00:00:00Z'))
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', (
                identity, jid, 'synthetic-sender', 'PRIVATE_SYNTHETIC_BODY_' + identity,
                '2026-09-25T00:00:00Z', 0, ''))

    def request(self, path, data=None, *, authenticated=True, csrf=True):
        headers = {}
        if authenticated:
            headers['Cookie'] = self.server.cookie_name + '=' + self.server.session_token
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': self.origin})
            if csrf:
                headers['X-CSRF-Token'] = self.server.csrf_token
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('POST' if data is not None else 'GET', path,
                               json.dumps(data).encode() if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def scan(self):
        status, result = self.request('/api/customers/scan', {})
        self.assertEqual(status, 200, result)
        return result

    def message_count(self):
        store = self.ws.open_store()
        try:
            return store.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        finally:
            store.close()

    def test_whatsapp_name_changes_reach_list_on_scan_and_sync_without_local_aliases(self):
        contacts = self.root / 'whatsapp.db'
        with closing(sqlite3.connect(contacts)) as db, db:
            db.execute('CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, '
                       'first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT)')
            db.execute('CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE)')
            db.execute('INSERT INTO whatsmeow_contacts VALUES (?,?,?,?,?,?)',
                       ('447700900099@s.whatsapp.net', self.jid_a, '', 'Dubai buyer', 'Buyer nickname', ''))
        before = self.message_count()
        scanned = self.scan()
        row = next(r for r in scanned['items'] if r['identifier'] == self.jid_a)
        self.assertEqual(row['display_name'], 'Dubai buyer')
        status, listing = self.request('/api/conversations')
        self.assertEqual(status, 200)
        self.assertEqual(listing['items'][0]['title'], 'Dubai buyer')
        self.assertEqual(self.message_count(), before)
        with closing(sqlite3.connect(contacts)) as db, db:
            db.execute('UPDATE whatsmeow_contacts SET full_name=?', ('Dubai buyer updated',))
        status, updated = self.request('/api/sync', {})
        self.assertEqual((status, updated['status']), (200, 'succeeded'))
        self.assertEqual(updated['inserted'], 1)
        self.assertEqual(self.request('/api/conversations')[1]['items'][0]['title'], 'Dubai buyer updated')
        # A selection issued before refresh cannot restore stale names.
        self.assertEqual(self.request('/api/customers/selection',
            {'scan_id': scanned['scan_id'], 'selected_ids': [row['id']]})[0], 409)
        self.assertEqual(self.scan()['selected_count'], 1)
        with closing(sqlite3.connect(contacts)) as db, db:
            db.execute('UPDATE whatsmeow_contacts SET full_name=?', ('',))
        self.assertEqual(next(r for r in self.scan()['items'] if r['identifier'] == self.jid_a)['display_name'], 'Buyer nickname')

    def test_repeated_scan_discovers_new_conversations_without_importing_message_bodies(self):
        first = self.scan()
        self.assertEqual({row['display_name'] for row in first['items']}, {'Synthetic Alice', 'Synthetic Bob'})
        self.assertNotIn('PRIVATE_SYNTHETIC_BODY', json.dumps(first))
        self.assertNotIn(str(self.source), json.dumps(first))
        self.assertEqual(self.message_count(), 0)
        self.add_chat('447700900003@s.whatsapp.net', 'Synthetic Carol', 'c1')
        second = self.scan()
        self.assertNotEqual(first['scan_id'], second['scan_id'])
        self.assertEqual(len(second['items']), 3)
        self.assertEqual(self.message_count(), 0)
        self.assertIsNone(self.server.active_job())

    def test_selection_then_update_imports_only_chosen_customers_and_pause_preserves_history(self):
        scanned = self.scan()
        bob = next(row for row in scanned['items'] if row['display_name'] == 'Synthetic Bob')
        status, saved = self.request('/api/customers/selection', {'scan_id': scanned['scan_id'], 'selected_ids': [bob['id']]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.message_count(), 0)
        self.assertEqual(self.request('/api/sync', {})[1]['inserted'], 1)
        with self.server.service() as service:
            bodies = [row[0] for row in service.store.connection.execute('SELECT body FROM messages')]
        self.assertEqual(bodies, ['PRIVATE_SYNTHETIC_BODY_b1'])
        repeated = self.scan()
        states = {row['display_name']: row['status'] for row in repeated['items']}
        self.assertEqual(states, {'Synthetic Alice': 'paused', 'Synthetic Bob': 'active'})
        status, paused = self.request('/api/customers/selection', {'scan_id': repeated['scan_id'], 'selected_ids': []})
        self.assertEqual(status, 200, paused)
        self.add_chat(self.jid_b, 'Synthetic Bob', 'b2')
        self.assertEqual(self.request('/api/sync', {})[1]['inserted'], 0)
        self.assertEqual(self.message_count(), 1)

    def test_stale_scan_and_unknown_selection_cannot_expand_scope(self):
        first = self.scan()
        second = self.scan()
        before = (self.ws.root / CONFIG_FILE).read_bytes()
        status, _ = self.request('/api/customers/selection', {'scan_id': first['scan_id'], 'selected_ids': []})
        self.assertEqual(status, 409)
        status, _ = self.request('/api/customers/selection', {'scan_id': second['scan_id'], 'selected_ids': ['invented-id']})
        self.assertIn(status, (400, 409))
        self.assertEqual((self.ws.root / CONFIG_FILE).read_bytes(), before)

    def test_customer_endpoints_require_local_session_and_csrf(self):
        for path, data in (('/api/customers', None), ('/api/customers/scan', {}),
                           ('/api/customers/selection', {'scan_id': 'missing', 'selected_ids': []})):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, data, authenticated=False)[0], 401)
                if data is not None:
                    self.assertEqual(self.request(path, data, csrf=False)[0], 403)
        self.assertEqual(self.request('/api/customers/scan', {'messages_db': '/unapproved.db'})[0], 400)
        self.assertEqual(self.request('/api/customers?workspace=other')[0], 400)

    def test_status_and_workspace_reload_preserve_customer_selection(self):
        scanned = self.scan()
        selected = [row['id'] for row in scanned['items']]
        self.assertEqual(self.request('/api/customers/selection', {'scan_id': scanned['scan_id'], 'selected_ids': selected})[0], 200)
        status, current = self.request('/api/customers')
        self.assertEqual(status, 200, current)
        self.assertEqual(current['selected_count'], 2)
        from inquiry_product.customer_management import customer_status
        reloaded = customer_status(Workspace.load(self.ws.root))
        self.assertEqual(reloaded['selected_count'], 2)

    def test_customer_status_reports_busy_while_a_sync_holds_the_scope_lock(self):
        from inquiry_product.manual_sync import _sync_lock
        with _sync_lock(self.ws):
            status, result = self.request('/api/customers')
        self.assertEqual(status, 409, result)


class CustomerLabelTests(unittest.TestCase):
    def test_paused_demo_customer_remains_identifiable_in_the_inbox(self):
        from inquiry_product.customer_management import scan_customers, save_customers
        from inquiry_product.web_demo import seed_workspace
        with tempfile.TemporaryDirectory() as folder:
            ws = seed_workspace(Path(folder) / 'demo')
            scan = scan_customers(ws)
            save_customers(ws, scan['scan_id'], [row['id'] for row in scan['items']
                           if row['selected'] and row['identifier'] != 'workbench:new-paper'])
            server = create_server(ws, port=0, model_catalog_provider=lambda: {'models': []})
            try:
                customer = next(row for row in server.conversations() if row['conversation_id'] == 'workbench:new-paper')
                self.assertIn('暂停', customer['subtitle'])
                self.assertEqual(customer['message_count'], 3)
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()
