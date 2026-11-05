import unittest

from chat_links import chat_url, open_command, UnsupportedChatLink


class ChatLinkTests(unittest.TestCase):
    def test_codex_selects_named_existing_chat_in_explicit_app(self):
        session = {"provider": "codex", "id": "11111111-1111-4111-8111-111111111111"}
        self.assertEqual(open_command(session), ["/usr/bin/open", "-b", "com.openai.codex",
                         "codex://threads/11111111-1111-4111-8111-111111111111"])

    def test_claude_uses_exact_local_desktop_session(self):
        url = "claude://code/continue?session=local_22222222-2222-4222-8222-222222222222"
        self.assertEqual(open_command({"provider": "claude", "open_url": url}),
                         ["/usr/bin/open", "-b", "com.anthropic.claudefordesktop", url])

    def test_missing_claude_mapping_never_silently_opens_generic_app(self):
        with self.assertRaises(UnsupportedChatLink):
            open_command({"provider": "claude", "id": "uuid", "can_open": True})

    def test_project_link_selects_the_exact_existing_thread(self):
        url = "claude://code/project/chan_123456789?thread=cmsg_123456789"
        self.assertEqual(chat_url({"provider": "claude", "open_url": url}), url)
        self.assertEqual(chat_url({"provider": "claude", "open_url": url.split("?")[0]}), url.split("?")[0])
        for invalid in (url + "&prompt=hello", url + "&thread=cmsg_other000", url + "#new",
                        url.replace("cmsg_123456789", "new"), "claude://code/project/new"):
            with self.subTest(invalid=invalid), self.assertRaises(UnsupportedChatLink):
                chat_url({"provider": "claude", "open_url": invalid})

    def test_links_cannot_create_chats_submit_prompts_or_open_external_content(self):
        values = (
            "https://example.com/", "claude://code/new", "claude://code/continue?session=local_bad",
            "claude://code/continue?session=local_22222222-2222-4222-8222-222222222222&prompt=hello",
            "claude://code/continue?session=local_22222222-2222-4222-8222-222222222222&session=local_22222222-2222-4222-8222-222222222222",
            "claude://user@code/continue?session=local_22222222-2222-4222-8222-222222222222",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(UnsupportedChatLink):
                chat_url({"provider": "claude", "open_url": value})
        for ident in ("new", "--help", "../new", "id?prompt=hello", ""):
            if ident == "--help":
                # This remains a harmless URL path, never a subprocess option.
                self.assertTrue(chat_url({"provider": "codex", "id": ident}).endswith("/--help"))
            else:
                with self.subTest(ident=ident), self.assertRaises(UnsupportedChatLink):
                    chat_url({"provider": "codex", "id": ident})


if __name__ == "__main__":
    unittest.main()
