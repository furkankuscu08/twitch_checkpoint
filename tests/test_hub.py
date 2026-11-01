import asyncio
from dataclasses import replace

from checkpoint.core.config import Config


def test_full_and_slow_clients_drop_without_blocking_healthy_clients():
    from checkpoint.server.hub import Hub

    class Socket:
        def __init__(self, slow=False):
            self.sent, self.closed, self.slow = [], False, slow
        async def send_json(self, item):
            if self.slow:
                await asyncio.Future()
            self.sent.append(item)
        async def close(self, **kwargs):
            self.closed = True

    async def run():
        hub = Hub(replace(Config(), WS_SEND_TIMEOUT_S=0.02))
        slow, full, good = Socket(True), Socket(), Socket()
        a = hub.add(slow, 'overlay', {'type': 'snapshot'})
        b = hub.add(full, 'overlay', {'type': 'snapshot'})
        assert b.queue.maxsize == 100
        for _ in range(99):
            b.queue.put_nowait({'type': 'queued'})
        hub.broadcast({'type': 'queued'}, 'overlay')
        hub.add(good, 'overlay', {'type': 'snapshot'})
        await asyncio.sleep(0.04)
        assert slow.closed and full.closed
        assert a not in hub.clients and b not in hub.clients
        hub.broadcast({'type': 'summary'}, 'overlay')
        await asyncio.sleep(0)
        assert good.sent[-1]['type'] == 'summary'
        await hub.close()
        assert good.closed
    asyncio.run(run())
