import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from test_api_manager_urls import PACKAGE, load_api_manager

load_api_manager()
module = importlib.import_module(PACKAGE + ".video_api_manager")
VideoApiManager, VideoError = module.VideoApiManager, module.VideoError
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2" + b"\0" * 32


class VideoPayloadTest(unittest.TestCase):
    def manager(self, mode, model="minimax_h3_lightx2v_no_pic", **extra):
        return VideoApiManager(
            dict(
                interface_mode=mode,
                model=model,
                duration=1,
                resolution="480p",
                aspect_ratio="16:9",
                **extra,
            ),
            Path("unused"),
        )

    def test_openai_reference_shape_and_seconds(self):
        m = self.manager("openai_video", "minimax_h3_lightx2v_v5")
        create, _query, body, _headers = m.build_request(
            "hello", ["image1", "image2"], [], []
        )
        self.assertEqual(create, "/v1/videos")
        self.assertEqual(body["seconds"], 1)
        self.assertEqual(
            body["input_reference"], [{"image_url": "image1"}, {"image_url": "image2"}]
        )
        self.assertNotIn("stream", body)

    def test_minimax_first_last_frame_roles(self):
        m = self.manager("minimax", "minimax_h3_lightx2v")
        _, _, body, _ = m.build_request("hello", ["a", "b"], [], [])
        self.assertEqual(body["resolution"], "480P")
        self.assertEqual(body["ratio"], "adaptive")
        self.assertEqual(
            [x["role"] for x in body["content"][1:]], ["first_frame", "last_frame"]
        )

    def test_native_uses_model_in_path_and_native_resolution(self):
        m = self.manager("autodl_native", "minimax_h3_z0902")
        create, _, body, _ = m.build_request("hello", ["a"], [], [])
        self.assertEqual(create, "/api/v1/comfyui/comfyui_workflow/minimax_h3_z0902")
        self.assertNotIn("model", body)
        self.assertEqual(body["resolution"], "480p横(864*480)")
        self.assertEqual(body["ref_image_0"], "a")

    def test_audio_sync_has_audio_duration_and_no_prompt(self):
        for mode in ("openai_video", "minimax", "autodl_native"):
            m = self.manager(mode, "minimax_h3_image_audio_to_video")
            _, _, body, _ = m.build_request("ignored", ["a"], ["b"], [])
            self.assertNotIn("prompt", body)
            self.assertNotIn("duration", body)
            self.assertNotIn("seed", body)
            self.assertEqual(
                body["seconds" if mode == "openai_video" else "audio_duration"], 1
            )

    def test_dashscope_accepts_legacy_and_generic_wan_models(self):
        legacy = self.manager("dashscope", "wan2.2-animate-move", use_stream=True)
        _, _, body, headers = legacy.build_request("ignored", ["a"], [], ["b"])
        self.assertEqual(set(body), {"model", "input", "parameters"})
        self.assertEqual(body["parameters"], {"mode": "wan-std", "check_image": True})
        self.assertEqual(headers["X-DashScope-Async"], "enable")
        self.assertNotIn("stream", body)

        generic = self.manager("dashscope", "wan3.0-video", use_stream=True)
        _, _, body, headers = generic.build_request(
            "人物向镜头挥手", ["a", "b"], ["c"], ["d"]
        )
        self.assertEqual(body["model"], "wan3.0-video")
        self.assertEqual(body["input"]["img_url"], "a")
        self.assertEqual(body["input"]["image_urls"], ["a", "b"])
        self.assertEqual(body["input"]["audio_url"], "c")
        self.assertEqual(body["input"]["video_url"], "d")
        self.assertEqual(body["input"]["prompt"], "人物向镜头挥手")
        self.assertEqual(headers["X-DashScope-Async"], "enable")

    def test_autodl_accepts_forward_compatible_workflow_id(self):
        m = self.manager("autodl_native", "my-custom-wan-model")
        create, _, body, _ = m.build_request("hello", ["a"], ["b"], ["c"])
        self.assertEqual(create, "/api/v1/comfyui/comfyui_workflow/my-custom-wan-model")
        self.assertEqual(body["prompt"], "hello")
        self.assertEqual(body["duration"], 1)
        self.assertEqual(body["ref_image_0"], "a")
        self.assertEqual(body["ref_audio_0"], "b")
        self.assertEqual(body["ref_video_0"], "c")

    def test_forward_compatible_model_can_use_media_without_prompt(self):
        for mode in ("openai_video", "minimax", "autodl_native", "seedance"):
            with self.subTest(mode):
                m = self.manager(mode, "provider-model-with-media-input")
                _create, _query, body, _headers = m.build_request("", ["image"], [], [])
                if mode == "openai_video":
                    self.assertEqual(body["input_reference"], {"image_url": "image"})
                elif mode == "minimax" or mode == "seedance":
                    self.assertTrue(body["content"])
                else:
                    self.assertEqual(body["ref_image_0"], "image")

    def test_seedance_reference_media_and_succeeded_result(self):
        m = self.manager("seedance", "doubao-seedance-1-0-pro", generate_audio=True)
        _, query, body, _ = m.build_request("hello", ["a", "b"], ["c"], ["d"])
        self.assertEqual(query, "/api/v3/contents/generations/tasks/{task_id}")
        self.assertEqual(len(body["content"]), 5)
        self.assertTrue(body["generate_audio"])
        self.assertEqual(
            m.parse_task(
                {
                    "id": "x",
                    "status": "succeeded",
                    "content": {"video_url": "https://media.test/v.mp4"},
                }
            )[:3],
            ("x", "succeeded", "https://media.test/v.mp4"),
        )

    def test_custom_template_typed_tokens_and_response_paths(self):
        m = self.manager(
            "custom_endpoint",
            custom_body_template=json.dumps(
                {
                    "description": "{prompt}",
                    "seconds": "{duration}",
                    "pictures": "{images}",
                    "stream": "{stream}",
                }
            ),
            custom_id_path="output.id",
            custom_status_path="output.state",
            custom_url_path="output.results.0.url",
            use_stream=True,
        )
        _, _, body, _ = m.build_request('quotes " and \n newline', ["a"], [], [])
        self.assertEqual(body["seconds"], 1)
        self.assertEqual(body["pictures"], ["a"])
        self.assertIs(body["stream"], True)
        self.assertEqual(
            m.parse_task(
                {
                    "output": {
                        "id": "t",
                        "state": "done",
                        "results": [{"url": "https://media.test/v.mp4"}],
                    }
                }
            )[:3],
            ("t", "done", "https://media.test/v.mp4"),
        )

    def test_model_media_constraints_reject_before_paid_submission(self):
        with self.assertRaises(VideoError):
            self.manager("openai_video").build_request(
                "hello", ["must not silently drop"], [], []
            )
        with self.assertRaises(VideoError):
            self.manager("minimax", "minimax_h3_lightx2v").build_request(
                "hello", ["a"], [], []
            )
        with self.assertRaises(VideoError):
            self.manager("minimax", "minimax_h3_lightx2v_v5").build_request(
                "hello", ["a"], [], [], 15
            )

    def test_invalid_natural_duration_ratio_and_exact_size_are_not_silently_changed(
        self,
    ):
        for prompt in ("猫 -1秒", "猫 1.5秒", "猫 2:1"):
            with self.subTest(prompt), self.assertRaises(VideoError):
                self.manager("openai_video").build_request(prompt, [], [], [])
        with self.assertRaises(VideoError):
            self.manager("openai_video", size="1280x720").build_request(
                "cloud", [], [], []
            )

    def test_explicit_quality_overrides_take_precedence_and_tasks_are_isolated(self):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (1, 1)).save(buffer, "PNG")
        m = self.manager("openai_video")
        _, prepared = m.prepare(
            "猫 768p 9:16",
            [buffer.getvalue()],
            [],
            [],
            options=module.VideoOptions(resolution="480p", aspect_ratio="16:9"),
        )
        _, _, body, _ = prepared.build_request("猫 768p 9:16", ["image"], [], [])
        self.assertEqual(body["model"], "minimax_h3_lightx2v_v5")
        self.assertEqual(
            (body["resolution"], body["orientation"]), ("480p", "landscape")
        )
        self.assertEqual(m.config["model"], "minimax_h3_lightx2v_no_pic")

    def test_documented_official_alias_constraints_reject_incompatible_frame_resolution(
        self,
    ):
        m = self.manager("minimax", "MiniMax-H3", reference_mode="first_last_frame")
        m.config.update(duration=5, resolution="1440p")
        with self.assertRaises(VideoError):
            m.build_request("hello", ["first", "last"], [], [])

    def test_base_url_normalization_and_prompt_overrides(self):
        for base in (
            "https://api.test",
            "https://api.test/v1",
            "https://api.test/v1/videos",
        ):
            self.assertEqual(
                module.endpoint(base, "/v1/videos"), "https://api.test/v1/videos"
            )
        self.assertEqual(
            module.endpoint(
                "https://ark.test/api/v3", "/api/v3/contents/generations/tasks"
            ),
            "https://ark.test/api/v3/contents/generations/tasks",
        )
        self.assertEqual(
            module.endpoint("https://api.test/proxy", "/v1/videos"),
            "https://api.test/proxy/v1/videos",
        )
        p = module.video_params("一秒? 实际 1秒 768p 9:16", {"duration": 5})
        self.assertEqual(
            (p["duration"], p["resolution"], p["ratio"]), (1, "768p", "9:16")
        )
        self.assertEqual(
            module.media_sources("data:video/mp4;base64,YQ=="),
            ["data:video/mp4;base64,YQ=="],
        )


class VideoLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = []
        self.polls = 0
        self.fail = False
        self.sse = False
        self.app = web.Application()
        self.app.router.add_post("/v1/videos", self.create)
        self.app.router.add_get("/v1/videos/task", self.poll)
        self.app.router.add_get("/result.mp4", self.download)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.manager = VideoApiManager(
            {
                "interface_mode": "openai_video",
                "base_url": self.base,
                "api_keys": "do-not-send-to-media",
                "model": "minimax_h3_lightx2v_no_pic",
                "duration": 1,
                "resolution": "480p",
                "aspect_ratio": "16:9",
                "poll_interval": 1,
                "poll_timeout": 10,
                "use_stream": True,
            },
            Path(self.tmp.name),
        )

    async def asyncTearDown(self):
        if self.manager._session:
            await self.manager._session.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def create(self, request):
        self.requests.append(await request.json())
        if self.fail:
            return web.json_response({"error": "rejected"}, status=500)
        if self.sse:
            return web.Response(
                text='data: {"id":"task","status":"queued"}\n\ndata: {"heartbeat":true}\n\n',
                content_type="text/event-stream",
            )
        return web.json_response({"id": "task", "status": "queued"})

    async def poll(self, request):
        self.polls += 1
        if self.polls == 1:
            return web.json_response({"id": "task", "status": "in_progress"})
        return web.json_response(
            {"id": "task", "status": "completed", "url": self.base + "/result.mp4"}
        )

    async def download(self, request):
        self.assertNotIn("Authorization", request.headers)
        return web.Response(body=MP4, content_type="video/mp4")

    async def test_submit_once_poll_twice_download_without_key_and_record(self):
        result = await self.manager.generate("hello", [])
        self.assertEqual(result.path.read_bytes(), MP4)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.polls, 2)
        self.assertNotIn("stream", self.requests[0])
        self.assertEqual(self.requests[0]["seconds"], 1)
        info = json.loads(next(self.manager.data_dir.glob("*.json")).read_text("utf-8"))
        self.assertEqual(info["task_id"], "task")
        self.assertEqual(info["status"], "completed")
        self.assertNotIn("do-not-send-to-media", json.dumps(info))

    async def test_sse_heartbeat_does_not_lose_task_id(self):
        self.sse = True
        result = await self.manager.generate("hello", [])
        self.assertEqual(result.task_id, "task")
        self.assertEqual(len(self.requests), 1)

    async def test_failed_submission_is_never_retried(self):
        self.fail = True
        with self.assertRaises(VideoError):
            await self.manager.generate("hello", [])
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_query_template_does_not_submit(self):
        self.manager.config.update(
            interface_mode="custom_endpoint",
            custom_query_path="/missing-placeholder",
            custom_body_template="{}",
        )
        with self.assertRaises(VideoError):
            await self.manager.generate("hello", [])
        self.assertEqual(self.requests, [])

    async def test_poll_failure_keeps_task_id_and_record_without_resubmitting(self):
        original = VideoApiManager._json_request

        async def request(manager, method, *args):
            if method == "GET":
                raise VideoError("temporary")
            return await original(manager, method, *args)

        with (
            patch.object(VideoApiManager, "_json_request", request),
            self.assertRaisesRegex(VideoError, "task"),
        ):
            await self.manager.generate("hello", [])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(list(self.manager.data_dir.glob("*.json")))
