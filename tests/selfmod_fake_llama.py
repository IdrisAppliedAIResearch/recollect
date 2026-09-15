"""Deterministic llama-server stand-in: /props, /slots and pinned streaming.

Slot state follows the pinned request's lifecycle, so tests can distinguish
decoding from queued, serialized or prompt-only processing. No model runs.
"""

import asyncio
import copy
import json

import httpx


def chunk(text):
    event = {"id": "chatcmpl-fake",
             "choices": [{"index": 0, "delta": {"content": text}}]}
    return ("data: " + json.dumps(event) + "\n\n").encode()


class FakeLlama:
    def __init__(self, slots=3, *, tokens=40, delay=0.03, decode=True,
                 answer="ANSWER", serial=False, stop_delay=0.05, n_ctx=8192):
        self.tokens, self.delay, self.decode = tokens, delay, decode
        self.answer, self.stop_delay = answer, stop_delay
        self.state = [{"id": i, "n_ctx": n_ctx, "is_processing": False}
                      for i in range(slots)]
        self.props = {"total_slots": slots,
                      "default_generation_settings": {"n_ctx": n_ctx}}
        self.requests, self.tasks = [], []
        self.hold = None
        self.slots_error = None
        self.deferred = 0
        self._task = 0
        self._serial = asyncio.Lock() if serial else None

    def transport(self):
        return httpx.MockTransport(self.handle)

    async def handle(self, request):
        path = request.url.path
        if path == "/props":
            return httpx.Response(200, json=self.props)
        if path == "/slots":
            if self.slots_error is not None:
                raise self.slots_error
            return httpx.Response(200, json=copy.deepcopy(self.state))
        if path == "/metrics":
            return httpx.Response(200, text=(
                "# TYPE llamacpp:requests_deferred gauge\n"
                f"llamacpp:requests_deferred {self.deferred}\n"))
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            self.requests.append(body)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Generation(self, body))
        return httpx.Response(404)

    def idle(self, slot):
        state = self.state[slot]
        state["is_processing"] = False
        state.pop("id_task", None)
        state.pop("next_token", None)


class Generation(httpx.AsyncByteStream):
    def __init__(self, fake, body):
        self.fake, self.body, self.closed = fake, body, False
        self.slot = body.get("id_slot", 0)

    async def __aiter__(self):
        fake = self.fake
        if fake._serial is not None:
            await fake._serial.acquire()
        state = fake.state[self.slot]
        fake._task += 1
        state.update(is_processing=True, id_task=fake._task,
                     next_token=[{"n_decoded": 0}])
        finished = False
        try:
            for _ in range(fake.tokens):
                if self.closed:
                    return
                await asyncio.sleep(fake.delay)
                if fake.decode:
                    state["next_token"][0]["n_decoded"] += 1
                yield chunk("x")
            if fake.hold is not None:
                await fake.hold.wait()
            yield chunk(fake.answer)
            yield b"data: [DONE]\n\n"
            finished = True
        finally:
            if finished:
                fake.idle(self.slot)
            if fake._serial is not None:
                fake._serial.release()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True

        async def stop():
            # The server notices the disconnect and frees the slot a bit later.
            await asyncio.sleep(self.fake.stop_delay)
            self.fake.idle(self.slot)

        self.fake.tasks.append(asyncio.create_task(stop()))
