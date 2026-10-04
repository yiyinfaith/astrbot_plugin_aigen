import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    """Load ImageManager without requiring a local AstrBot installation."""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")

    class AstrMessageEvent:
        pass

    class Image:
        def __init__(self, source):
            self.url = source
            self.file = None
            self.path = None

    class Reply:
        def __init__(self, chain=None, reply_id=None):
            self.chain = chain or []
            self.id = reply_id

    class At:
        def __init__(self, qq):
            self.qq = str(qq)

    event_module.AstrMessageEvent = AstrMessageEvent
    components.Image = Image
    components.Reply = Reply
    components.At = At
    api.event = event_module
    api.message_components = components
    api.logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_module,
            "astrbot.api.message_components": components,
        }
    )
    return Image, Reply, At


Image, Reply, At = _install_astrbot_stubs()
sys.path.insert(0, str(ROOT))
from image_manager import ImageManager  # noqa: E402


class FakeEvent:
    def __init__(self, chain, sender_id="99", text=""):
        self._chain = chain
        self._sender_id = sender_id
        self.message_str = text

    def get_messages(self):
        return self._chain

    def get_message_str(self):
        return self.message_str

    def get_sender_id(self):
        return self._sender_id


class ImageInputOrderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = ImageManager({})
        self.avatars_requested = []

        async def load_bytes(source):
            return f"image:{source}".encode()

        async def get_avatar(user_id):
            self.avatars_requested.append(str(user_id))
            return f"avatar:{user_id}".encode()

        self.manager.load_bytes = load_bytes
        self.manager.get_avatar = get_avatar

    async def test_single_image_uses_quote_before_sent_at_and_sender(self):
        event = FakeEvent(
            [
                Image("https://sent"),
                At("2"),
                Reply([Image("https://quoted")]),
            ]
        )
        result = await self.manager.extract_images_from_event(
            event, max_images=1, include_sender_avatar=True
        )
        self.assertEqual(result, [b"image:https://quoted"])
        self.assertEqual(self.avatars_requested, [])

    async def test_single_image_falls_back_to_sender_avatar(self):
        result = await self.manager.extract_images_from_event(
            FakeEvent([]), max_images=1, include_sender_avatar=True
        )
        self.assertEqual(result, [b"avatar:99"])
        self.assertEqual(self.avatars_requested, ["99"])

    async def test_single_image_uses_sent_image_before_at_avatar(self):
        result = await self.manager.extract_images_from_event(
            FakeEvent([At("2"), Image("https://sent")]),
            max_images=1,
            include_sender_avatar=True,
        )
        self.assertEqual(result, [b"image:https://sent"])
        self.assertEqual(self.avatars_requested, [])

    async def test_single_image_uses_at_avatar_before_sender_avatar(self):
        result = await self.manager.extract_images_from_event(
            FakeEvent([At("2")]), max_images=1, include_sender_avatar=True
        )
        self.assertEqual(result, [b"avatar:2"])
        self.assertEqual(self.avatars_requested, ["2"])

    async def test_multi_image_keeps_component_order_and_skips_sender_avatar(self):
        event = FakeEvent(
            [
                Image("https://sent-1"),
                At("2"),
                Reply([Image("https://quoted")]),
                Image("https://sent-2"),
            ]
        )
        result = await self.manager.extract_images_from_event(event)
        self.assertEqual(
            result,
            [
                b"image:https://sent-1",
                b"avatar:2",
                b"image:https://quoted",
                b"image:https://sent-2",
            ],
        )
        self.assertEqual(self.avatars_requested, ["2"])


if __name__ == "__main__":
    unittest.main()
