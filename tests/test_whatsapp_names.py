"""Synthetic WhatsApp contact metadata: names remain owned by WhatsApp."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from inquiry_product.whatsapp_names import resolve_names


class WhatsAppNamesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.messages = self.root / "messages.db"
        self.messages.write_bytes(b"synthetic message database; never opened")
        self.contacts = self.root / "whatsapp.db"
        self.config = {"messages_db": str(self.messages)}
        self.pn = "447700900001@s.whatsapp.net"
        self.lid = "100000000001@lid"
        self.owner = "447700900099@s.whatsapp.net"

    def database(self, mapping=True):
        with closing(sqlite3.connect(self.contacts)) as db, db:
            db.execute("CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT, PRIMARY KEY (our_jid, their_jid))")
            if mapping:
                db.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE)")

    def contact(self, jid=None, full="", first="", push="", business="", owner=None):
        with closing(sqlite3.connect(self.contacts)) as db, db:
            db.execute("INSERT OR REPLACE INTO whatsmeow_contacts VALUES (?,?,?,?,?,?)", (owner or self.owner, jid or self.pn, first, full, push, business))

    def map_lid(self):
        with closing(sqlite3.connect(self.contacts)) as db, db:
            db.execute("INSERT INTO whatsmeow_lid_map VALUES (?,?)", ("100000000001", "447700900001"))

    def test_whatsapp_saved_name_precedes_first_name_nickname_and_business_name(self):
        self.database()
        self.contact(full="Dubai buyer", first="Buyer", push="Alice", business="Textiles")
        self.assertEqual(resolve_names(self.config, {self.pn: "447700900001"}), {self.pn: "Dubai buyer"})

    def test_name_changes_are_visible_without_restarting_or_mutating_input(self):
        self.database()
        self.contact(full="Before")
        source = {self.pn: "Cached old name"}
        self.assertEqual(resolve_names(self.config, source)[self.pn], "Before")
        self.contact(full="After")
        self.assertEqual(resolve_names(self.config, source)[self.pn], "After")
        self.assertEqual(source, {self.pn: "Cached old name"})

    def test_clearing_contact_names_does_not_resurrect_old_chat_name(self):
        self.database()
        self.contact(full="Before")
        source = {self.pn: "Cached old name"}
        self.assertEqual(resolve_names(self.config, source)[self.pn], "Before")
        self.contact()
        self.assertEqual(resolve_names(self.config, source)[self.pn], "+447700900001")

    def test_fallback_order_first_name_then_nickname_then_business_then_phone(self):
        self.database()
        for fields, wanted in [({"first": "Saved first", "push": "Nickname", "business": "Company"}, "Saved first"), ({"push": "Nickname", "business": "Company"}, "Nickname"), ({"business": "Company"}, "Company"), ({}, "+447700900001")]:
            with self.subTest(fields=fields):
                self.contact(**fields)
                self.assertEqual(resolve_names(self.config, {self.pn: "Old"})[self.pn], wanted)

    def test_lid_and_phone_aliases_share_saved_name(self):
        self.database()
        self.map_lid()
        self.contact(full="Saved on phone")
        self.assertEqual(resolve_names(self.config, {self.lid: "100000000001", self.pn: "447700900001"}), {self.lid: "Saved on phone", self.pn: "Saved on phone"})

    def test_saved_alias_name_precedes_direct_nickname(self):
        self.database()
        self.map_lid()
        self.contact(full="Saved on phone")
        self.contact(jid=self.lid, push="Self chosen nickname")
        self.assertEqual(resolve_names(self.config, {self.lid: "Old"})[self.lid], "Saved on phone")

    def test_phone_can_resolve_name_stored_only_under_lid(self):
        self.database()
        self.map_lid()
        self.contact(jid=self.lid, full="Saved under LID")
        self.assertEqual(resolve_names(self.config, {self.pn: "447700900001"})[self.pn], "Saved under LID")

    def test_known_phone_name_is_consistent_for_phone_and_lid_when_both_have_saved_names(self):
        self.database()
        self.map_lid()
        self.contact(full="Address book name")
        self.contact(jid=self.lid, full="Older alias name")
        self.assertEqual(resolve_names(self.config, {self.pn: "Old", self.lid: "Old"}), {self.pn: "Address book name", self.lid: "Address book name"})

    def test_lid_fallback_uses_verified_phone_mapping_or_hides_internal_digits(self):
        self.assertEqual(resolve_names(self.config, {self.lid: "100000000001"})[self.lid], "未命名客户")
        self.database()
        self.map_lid()
        self.contact()
        self.assertEqual(resolve_names(self.config, {self.lid: self.lid})[self.lid], "+447700900001")

    def test_groups_keep_source_name_even_when_contact_table_has_a_name(self):
        self.database()
        group = "120363000000@g.us"
        self.contact(jid=group, full="Not the group title")
        self.assertEqual(resolve_names(self.config, {group: "Logistics team"}), {group: "Logistics team"})

    def test_missing_database_preserves_usable_source_names_and_never_creates_files(self):
        self.assertEqual(resolve_names(self.config, {self.pn: "Known source name", self.lid: "100000000001"}), {self.pn: "Known source name", self.lid: "未命名客户"})
        self.assertFalse(self.contacts.exists())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"messages.db"})

    def test_missing_contact_keeps_source_name_instead_of_unknown_identity(self):
        self.database()
        self.contact(jid="447700900002@s.whatsapp.net", full="Unrequested contact")
        self.assertEqual(resolve_names(self.config, {self.pn: "Known source name"}), {self.pn: "Known source name"})

    def test_usable_name_with_at_sign_is_not_treated_as_raw_jid(self):
        self.assertEqual(resolve_names(self.config, {self.pn: "buyer@example.test"}), {self.pn: "buyer@example.test"})

    def test_raw_jid_or_formatted_number_in_cache_does_not_become_a_name(self):
        self.assertEqual(resolve_names(self.config, {self.pn: self.pn, self.lid: "+100 (000) 000-001"}), {self.pn: "+447700900001", self.lid: "未命名客户"})

    def test_multiple_accounts_never_mix_their_contact_names_or_aliases(self):
        self.database()
        self.map_lid()
        self.contact(full="Owner one name")
        self.contact(full="Owner two name", owner="447700900098@s.whatsapp.net")
        self.assertEqual(resolve_names(self.config, {self.pn: "Source name", self.lid: "100000000001"}), {self.pn: "Source name", self.lid: "未命名客户"})

    def test_unsupported_or_corrupt_database_is_only_a_name_fallback(self):
        self.contacts.write_bytes(b"not sqlite")
        self.assertEqual(resolve_names(self.config, {self.pn: "447700900001"})[self.pn], "+447700900001")
        self.contacts.unlink()
        with closing(sqlite3.connect(self.contacts)) as db, db:
            db.execute("CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT)")
        self.assertEqual(resolve_names(self.config, {self.pn: "Source name"})[self.pn], "Source name")

    def test_direct_contact_names_work_without_optional_mapping_table(self):
        self.database(mapping=False)
        self.contact(full="Direct contact")
        self.assertEqual(resolve_names(self.config, {self.pn: "Old"})[self.pn], "Direct contact")

    def test_names_remove_control_characters_and_bound_display_length(self):
        self.database()
        self.contact(full="  Dubai\x00\x1b\u202e\u2066 buyer\t  ")
        self.assertEqual(resolve_names(self.config, {self.pn: "Old"})[self.pn], "Dubai buyer")
        self.contact(full="客" * 1000)
        self.assertEqual(resolve_names(self.config, {self.pn: "Old"})[self.pn], "客" * 120)

    def test_reading_names_changes_neither_database_and_returns_only_requested_jids(self):
        self.database()
        self.contact(full="Allowed name")
        self.contact(jid="447700900002@s.whatsapp.net", full="Unrequested contact")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(resolve_names(self.config, {self.pn: "Old"}), {self.pn: "Allowed name"})
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_symlinked_contacts_or_source_paths_do_not_redirect_contact_reads(self):
        self.database()
        self.contact(full="Must not read")
        real = self.root / "other.db"
        self.contacts.rename(real)
        self.contacts.symlink_to(real)
        self.assertEqual(resolve_names(self.config, {self.pn: "Source name"})[self.pn], "Source name")
        self.contacts.unlink()
        real.rename(self.contacts)
        source_alias = self.root / "alias.db"
        source_alias.symlink_to(self.messages)
        self.assertEqual(resolve_names({"messages_db": str(source_alias)}, {self.pn: "Source name"})[self.pn], "Source name")

    def test_bound_parent_directory_alias_still_resolves_its_sibling_contact_store(self):
        self.database()
        self.contact(full="Bound directory name")
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(resolve_names({"messages_db": str(linked / "messages.db")}, {self.pn: "Source name"})[self.pn], "Bound directory name")

    def test_native_temporary_directory_path_supports_macos_var_alias(self):
        self.database()
        self.contact(full="Native temporary path")
        native = str(Path(self.tmp.name) / "messages.db")
        self.assertEqual(resolve_names({"messages_db": native}, {self.pn: "Old"})[self.pn], "Native temporary path")

    def test_locked_database_falls_back_then_recovers_after_lock_is_released(self):
        self.database()
        self.contact(full="Recovered name")
        with closing(sqlite3.connect(self.contacts)) as db:
            db.execute("BEGIN EXCLUSIVE")
            self.assertEqual(resolve_names(self.config, {self.pn: "Source name"})[self.pn], "Source name")
            db.rollback()
        self.assertEqual(resolve_names(self.config, {self.pn: "Source name"})[self.pn], "Recovered name")

    def test_contact_store_outage_keeps_previous_source_name_then_refreshes_after_recovery(self):
        self.database()
        self.contact(full="First saved name")
        names = {self.pn: "447700900001"}
        previous = resolve_names(self.config, names)
        away = self.root / "temporarily-unavailable.db"
        self.contacts.rename(away)
        self.assertEqual(resolve_names(self.config, names, previous_names=previous), {self.pn: "First saved name"})
        away.rename(self.contacts)
        self.contact(full="Updated saved name")
        self.assertEqual(resolve_names(self.config, names, previous_names=previous), {self.pn: "Updated saved name"})
        self.contact()
        self.assertEqual(resolve_names(self.config, names, previous_names=previous), {self.pn: "+447700900001"})

    def test_locked_contact_store_keeps_previous_name_when_bridge_only_has_number(self):
        self.database()
        self.contact(full="Updated name")
        with closing(sqlite3.connect(self.contacts)) as db:
            db.execute("BEGIN EXCLUSIVE")
            self.assertEqual(resolve_names(self.config, {self.pn: "447700900001"}, previous_names={self.pn: "Previous name"}), {self.pn: "Previous name"})
            db.rollback()

    def test_contact_store_outage_does_not_hide_usable_changed_bridge_name(self):
        self.assertEqual(resolve_names(self.config, {self.pn: "New source name"}, previous_names={self.pn: "Old source name"}), {self.pn: "New source name"})

    def test_numeric_previous_names_do_not_turn_internal_lid_into_phone_or_name(self):
        for value in ("100000000001", self.lid, "+100000000001", ""):
            with self.subTest(value=value):
                self.assertEqual(resolve_names(self.config, {self.lid: "100000000001"}, previous_names={self.lid: value}), {self.lid: "未命名客户"})

    def test_successful_lookup_without_contact_row_uses_source_not_previous_name(self):
        self.database()
        self.contact(jid="447700900002@s.whatsapp.net", full="Unrequested contact")
        self.assertEqual(resolve_names(self.config, {self.pn: "447700900001"}, previous_names={self.pn: "Previous name"}), {self.pn: "+447700900001"})

    def test_more_than_sqlite_single_query_parameter_limit_keeps_all_requested_names(self):
        self.database()
        jids = {f"4477009{index:05d}@s.whatsapp.net": "Old" for index in range(1200)}
        with closing(sqlite3.connect(self.contacts)) as db, db:
            db.executemany("INSERT INTO whatsmeow_contacts VALUES (?,?,?,?,?,?)", [(self.owner, jid, "", "Saved buyer", "", "") for jid in jids])
        result = resolve_names(self.config, jids)
        self.assertEqual(len(result), 1200)
        self.assertEqual(set(result.values()), {"Saved buyer"})


if __name__ == "__main__":
    unittest.main()
