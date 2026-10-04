"""Worker supervision tests; no database or provider access."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch, call
from workers import supervise


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_recovers_after_repeated_failures_and_propagates_shutdown(self):
        attempts=0
        recovered=asyncio.Event()
        async def worker():
            nonlocal attempts
            attempts+=1
            if attempts<3:
                raise ConnectionError('temporary database outage')
            recovered.set()
            await asyncio.Event().wait()
        with patch('workers.asyncio.sleep',new_callable=AsyncMock) as sleep, patch('workers.logging.exception'):
            task=asyncio.create_task(supervise(worker,name='fixture'))
            try:
                await asyncio.wait_for(recovered.wait(),1)
                self.assertEqual(attempts,3)
                self.assertEqual(sleep.await_args_list,[call(1),call(2)])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(attempts,3)

    async def test_unexpected_return_does_not_permanently_stop_a_worker(self):
        attempts=0
        recovered=asyncio.Event()
        async def worker():
            nonlocal attempts
            attempts+=1
            if attempts==1:
                return
            recovered.set()
            await asyncio.Event().wait()
        with patch('workers.asyncio.sleep',new_callable=AsyncMock),patch('workers.logging.warning'):
            task=asyncio.create_task(supervise(worker,name='fixture'))
            try:
                await asyncio.wait_for(recovered.wait(),1)
                self.assertEqual(attempts,2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task


if __name__=='__main__':
    unittest.main()
