import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from connectlife.api import LifeConnectError
from capture import switchbot_control as controller

controller.log.disabled = True
controller.api_log.disabled = True


class FakeAppliance:
    def __init__(self):
        self.device_nickname = "Dehumidifier"
        self.status_list = {
            "t_power": 0,
            "t_work_mode": 1,
            "t_fan_speed": 2,
            "t_humidity": 30,
        }
        self.updates = []
        self.update_failures = 0
        self.refreshed_power = None
        self.refresh_error = None
        self.refresh_started = None
        self.release_refresh = None

    async def update_properties(self, properties):
        if self.update_failures:
            self.update_failures -= 1
            raise LifeConnectError("simulated update timeout")
        self.updates.append(dict(properties))
        self.status_list.update(properties)

    async def fetch_status(self):
        if self.refresh_started:
            self.refresh_started.set()
        if self.release_refresh:
            await self.release_refresh.wait()
        if self.refresh_error:
            raise self.refresh_error
        power = (
            self.refreshed_power
            if self.refreshed_power is not None
            else self.status_list["t_power"]
        )
        return {
            "statusList": {
                **self.status_list,
                "t_power": power,
            }
        }

    def _update_status(self, appliance_data):
        self.status_list = dict(appliance_data["statusList"])


class ControllerStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.appliance = FakeAppliance()
        controller._cl_api = object()
        controller._appliances = [self.appliance]
        controller._device_state = False
        controller._last_state_refresh = time.monotonic()
        controller._command_lock = asyncio.Lock()
        controller._connect_lock = asyncio.Lock()
        controller._last_auto_command_attempt = {True: 0.0, False: 0.0}
        controller._api_outage_active = False
        controller._api_failure_count = 0
        controller._api_last_error = ""
        controller._loop = None
        self.notify = patch.object(
            controller,
            "tg_notify",
            new=AsyncMock(),
        )
        self.notify.start()
        self.addCleanup(self.notify.stop)

    async def test_fresh_matching_off_state_skips_write(self):
        changed = await controller.set_device_power(False, "test")

        self.assertFalse(changed)
        self.assertEqual([], self.appliance.updates)

    async def test_stale_matching_state_does_not_skip_off_write(self):
        controller._last_state_refresh = (
            time.monotonic() - controller.STATE_REFRESH_INTERVAL - 1
        )

        changed = await controller.set_device_power(False, "humidity=50%")

        self.assertTrue(changed)
        self.assertEqual([{"t_power": 0}], self.appliance.updates)
        self.assertFalse(controller._device_state)
        self.assertTrue(controller.state_is_fresh())

    async def test_failed_on_then_recovered_off_does_not_need_status(self):
        self.appliance.update_failures = 1

        turned_on = await controller.set_device_power(True, "humidity=60%")

        self.assertFalse(turned_on)
        self.assertIsNone(controller._device_state)
        self.assertTrue(controller._api_outage_active)

        # The physical/API state can change while the outcome is unknown.
        self.appliance.status_list["t_power"] = 1
        turned_off = await controller.set_device_power(False, "humidity=50%")

        self.assertTrue(turned_off)
        self.assertEqual([{"t_power": 0}], self.appliance.updates)
        self.assertFalse(controller._device_state)
        self.assertFalse(controller._api_outage_active)

    async def test_get_timeout_invalidates_off_cache_and_recovery_turns_off(self):
        self.appliance.refresh_error = LifeConnectError(
            "ConnectLife request failed during get appliances: TimeoutError()"
        )

        with self.assertRaises(LifeConnectError):
            await controller.refresh_device_state(force=True)

        self.assertIsNone(controller._device_state)
        self.assertTrue(controller._api_outage_active)

        # The next low-humidity command must write OFF directly. It must not
        # need /status or another successful GET as a prerequisite.
        self.appliance.refresh_error = None
        self.appliance.status_list["t_power"] = 1
        turned_off = await controller.set_device_power(False, "humidity=50%")

        self.assertTrue(turned_off)
        self.assertEqual([{"t_power": 0}], self.appliance.updates)
        self.assertFalse(controller._device_state)

    async def test_refresh_corrects_external_state(self):
        self.appliance.refreshed_power = 1

        state = await controller.refresh_device_state(force=True)

        self.assertTrue(state)
        self.assertTrue(controller._device_state)
        self.assertTrue(controller.state_is_fresh())

    async def test_slow_refresh_does_not_block_or_overwrite_off_command(self):
        self.appliance.refreshed_power = 1
        self.appliance.refresh_started = asyncio.Event()
        self.appliance.release_refresh = asyncio.Event()
        refresh = asyncio.create_task(controller.refresh_for_status())
        await self.appliance.refresh_started.wait()

        controller._last_state_refresh = (
            time.monotonic() - controller.STATE_REFRESH_INTERVAL - 1
        )
        changed = await asyncio.wait_for(
            controller.set_device_power(False, "humidity=50%"),
            timeout=0.1,
        )
        self.appliance.release_refresh.set()
        await refresh

        self.assertTrue(changed)
        self.assertFalse(controller._device_state)
        self.assertEqual([{"t_power": 0}], self.appliance.updates)

    def test_queue_schedules_when_matching_state_is_stale(self):
        controller._loop = object()
        controller._last_state_refresh = (
            time.monotonic() - controller.STATE_REFRESH_INTERVAL - 1
        )

        def close_coroutine(coroutine, loop):
            coroutine.close()
            return None

        with patch.object(
            controller.asyncio,
            "run_coroutine_threadsafe",
            side_effect=close_coroutine,
        ) as scheduled:
            queued = controller.queue_auto_command(False, "humidity=50%")

        self.assertTrue(queued)
        scheduled.assert_called_once()


if __name__ == "__main__":
    unittest.main()
