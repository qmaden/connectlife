import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from connectlife.api import LifeConnectError
from capture import switchbot_control as controller

controller.log.disabled = True
controller.api_log.disabled = True


def close_coroutine(coroutine):
    coroutine.close()
    return None


class FakeAppliance:
    def __init__(self):
        self.puid = "target-puid"
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
        controller._sensor_offline = False
        controller._sensor_offline_pending = False
        controller._sensor_offline_generation = 0
        controller._last_sensor_data_time = time.monotonic()
        controller._start_time = time.monotonic() - controller.STARTUP_GRACE - 1
        controller._last_command_time = 0.0
        controller._last_command_state = None
        controller._mode = "auto"
        controller._first_data_seen = False
        controller._last_sensor.clear()
        controller._background_tasks.clear()

        settings_dir = tempfile.TemporaryDirectory()
        self.addCleanup(settings_dir.cleanup)
        self.settings_file = Path(settings_dir.name) / "settings.json"
        for patcher in (
            patch.object(controller, "SETTINGS_FILE", self.settings_file),
            patch.dict(controller._settings, controller.DEFAULT_SETTINGS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
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
        controller.tg_notify.assert_not_awaited()

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

    async def test_confirmed_on_to_off_transition_notifies(self):
        controller._device_state = True
        controller._last_state_refresh = time.monotonic()

        changed = await controller.set_device_power(False, "humidity=50%")

        self.assertTrue(changed)
        self.assertFalse(controller._device_state)
        controller.tg_notify.assert_awaited_once()

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

    def test_queue_suppresses_matching_state_while_reconcile_is_pending(self):
        controller._loop = object()
        controller._last_state_refresh = (
            time.monotonic() - controller.STATE_REFRESH_INTERVAL - 1
        )

        with patch.object(
            controller,
            "spawn",
            side_effect=close_coroutine,
        ) as scheduled:
            queued = controller.queue_auto_command(False, "humidity=50%")

        self.assertFalse(queued)
        scheduled.assert_not_called()

    def test_queue_schedules_after_refresh_failure_invalidates_state(self):
        controller._loop = object()
        controller._device_state = None

        with patch.object(
            controller,
            "spawn",
            side_effect=close_coroutine,
        ) as scheduled:
            queued = controller.queue_auto_command(False, "humidity=50%")

        self.assertTrue(queued)
        scheduled.assert_called_once()

    async def test_short_offline_event_is_cancelled_without_alarm(self):
        controller._loop = asyncio.get_running_loop()

        with patch.object(controller, "SENSOR_OFFLINE_CONFIRM_SECONDS", 0.03):
            controller.schedule_sensor_offline_confirmation()
            await asyncio.sleep(0.01)
            controller.mark_sensor_online()
            await asyncio.sleep(0.04)

        self.assertFalse(controller._sensor_offline)
        self.assertFalse(controller._sensor_offline_pending)
        controller.tg_notify.assert_not_awaited()

    async def test_persistent_offline_event_is_confirmed(self):
        controller._loop = asyncio.get_running_loop()

        with patch.object(controller, "SENSOR_OFFLINE_CONFIRM_SECONDS", 0.01):
            controller.schedule_sensor_offline_confirmation()
            await asyncio.sleep(0.02)

        self.assertTrue(controller._sensor_offline)
        self.assertFalse(controller._sensor_offline_pending)
        controller.tg_notify.assert_awaited_once()

    def test_multiple_appliances_require_stable_puid(self):
        other = FakeAppliance()
        other.puid = "other-puid"

        with (
            patch.object(controller, "TARGET_APPLIANCE_PUID", ""),
            self.assertRaisesRegex(RuntimeError, "Multiple ConnectLife appliances"),
        ):
            controller.select_target_appliance([other, self.appliance])

    def test_configured_puid_selects_target_regardless_of_order(self):
        other = FakeAppliance()
        other.puid = "other-puid"

        with patch.object(
            controller,
            "TARGET_APPLIANCE_PUID",
            self.appliance.puid,
        ):
            selected = controller.select_target_appliance(
                [other, self.appliance]
            )

        self.assertIs(self.appliance, selected)

    async def test_stale_read_right_after_command_is_ignored(self):
        turned_on = await controller.set_device_power(True, "humidity=60%")
        self.assertTrue(turned_on)

        # The gateway still serves the pre-command snapshot.
        self.appliance.refreshed_power = 0
        state = await controller.refresh_device_state(force=True)

        self.assertTrue(state)
        self.assertTrue(controller._device_state)

        # After the settle window a contradicting read is a real change.
        controller._last_command_time -= controller.COMMAND_SETTLE_SECONDS + 1
        state = await controller.refresh_device_state(force=True)

        self.assertFalse(state)
        self.assertFalse(controller._device_state)

    async def test_stale_retained_reading_does_not_drive_control(self):
        controller._loop = asyncio.get_running_loop()
        stale = {
            "humidity": 70,
            "timestamp": time.time() - controller.SENSOR_MAX_AGE_SECONDS - 60,
        }

        with patch.object(
            controller, "spawn", side_effect=close_coroutine
        ) as scheduled:
            controller.handle_mqtt_message(
                f"{controller.SENSOR_TOPIC}/json", json.dumps(stale)
            )
            scheduled.assert_not_called()
            self.assertEqual(70, controller._last_sensor["humidity"])
            self.assertFalse(controller._first_data_seen)

            fresh = {**stale, "timestamp": time.time()}
            controller.handle_mqtt_message(
                f"{controller.SENSOR_TOPIC}/json", json.dumps(fresh)
            )
            scheduled.assert_called_once()
            self.assertTrue(controller._first_data_seen)

    async def test_mode_change_is_persisted(self):
        self.assertTrue(controller.set_mode("manual", "test"))

        self.assertEqual("manual", controller._mode)
        self.assertEqual(
            "manual",
            json.loads(self.settings_file.read_text())["mode"],
        )
        self.assertEqual("manual", controller.load_settings()["mode"])
        self.assertFalse(controller.set_mode("bogus", "test"))

    async def test_switching_to_auto_applies_fresh_reading(self):
        controller._mode = "manual"
        controller._loop = asyncio.get_running_loop()
        controller._last_sensor.update(
            {"humidity": 70, "timestamp": time.time()}
        )

        with patch.object(
            controller, "spawn", side_effect=close_coroutine
        ) as scheduled:
            controller.set_mode("auto", "test")

        scheduled.assert_called_once()

    async def test_api_recovery_clears_auto_retry_throttle(self):
        controller._api_outage_active = True
        controller._last_auto_command_attempt[False] = time.monotonic()

        await controller.record_api_recovery("refresh state")

        self.assertEqual({True: 0.0, False: 0.0}, controller._last_auto_command_attempt)

    async def test_offline_failsafe_turns_device_off(self):
        controller._loop = asyncio.get_running_loop()
        controller._device_state = True

        with (
            patch.object(controller, "SENSOR_OFFLINE_CONFIRM_SECONDS", 0.01),
            patch.object(controller, "SENSOR_OFFLINE_FAILSAFE_SECONDS", 0.01),
        ):
            controller.schedule_sensor_offline_confirmation()
            await asyncio.sleep(0.05)

        self.assertEqual([{"t_power": 0}], self.appliance.updates)
        self.assertFalse(controller._device_state)

    async def test_sensor_recovery_cancels_offline_failsafe(self):
        controller._loop = asyncio.get_running_loop()
        controller._device_state = True

        with (
            patch.object(controller, "SENSOR_OFFLINE_CONFIRM_SECONDS", 0.01),
            patch.object(controller, "SENSOR_OFFLINE_FAILSAFE_SECONDS", 0.05),
        ):
            controller.schedule_sensor_offline_confirmation()
            await asyncio.sleep(0.02)
            self.assertTrue(controller._sensor_offline)
            controller.mark_sensor_online()
            await asyncio.sleep(0.06)

        self.assertEqual([], self.appliance.updates)
        self.assertTrue(controller._device_state)

    async def test_manual_telegram_command_forces_write_and_reports(self):
        controller._mode = "manual"
        query = {
            "id": "query",
            "data": "ctrl:off",
            "message": {"chat": {"id": 1}, "message_id": 2},
        }

        with (
            patch.object(controller, "tg_answer", new=AsyncMock()) as answer,
            patch.object(controller, "tg_edit", new=AsyncMock()) as edit,
        ):
            await controller.handle_callback(query)

        # The cached state was fresh and OFF, but a manual press still writes.
        self.assertEqual([{"t_power": 0}], self.appliance.updates)
        answer.assert_awaited_once()
        self.assertIn("confirmed", edit.await_args.args[2])

    async def test_manual_telegram_command_rejected_in_auto_mode(self):
        query = {
            "id": "query",
            "data": "ctrl:on",
            "message": {"chat": {"id": 1}, "message_id": 2},
        }

        with (
            patch.object(controller, "tg_answer", new=AsyncMock()) as answer,
            patch.object(controller, "tg_edit", new=AsyncMock()),
        ):
            await controller.handle_callback(query)

        self.assertEqual([], self.appliance.updates)
        answer.assert_awaited_once()

    async def test_updates_from_other_chats_are_ignored(self):
        with (
            patch.object(controller, "TELEGRAM_CHAT_ID", "123"),
            patch.object(controller, "handle_command", new=AsyncMock()) as command,
        ):
            await controller.dispatch_telegram_update(
                {"message": {"chat": {"id": 999}, "text": "/status"}}
            )
            command.assert_not_awaited()

            await controller.dispatch_telegram_update(
                {"message": {"chat": {"id": 123}, "text": "/status"}}
            )
            command.assert_awaited_once()

    async def test_telegram_poll_backs_off_on_every_failure(self):
        responses = [
            aiohttp.ClientConnectionError("refused"),
            aiohttp.ClientConnectionError("refused"),
            controller.TelegramApiError(
                "getUpdates",
                {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}},
            ),
            {"ok": True, "result": []},
            asyncio.CancelledError(),
        ]

        with (
            patch.object(
                controller, "_tg_request", new=AsyncMock(side_effect=responses)
            ),
            patch.object(controller.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await controller.telegram_poll()

        self.assertEqual(
            [5, 10, 7],
            [call.args[0] for call in sleep.await_args_list],
        )

    async def test_mqtt_messages_are_processed_on_the_event_loop(self):
        controller._loop = asyncio.get_running_loop()
        handled_on = []
        original = controller.handle_mqtt_message

        def record(topic, payload):
            handled_on.append(threading.get_ident())
            original(topic, payload)

        message = SimpleNamespace(
            topic=f"{controller.CONTROL_TOPIC}/mode",
            payload=b"manual",
        )
        with patch.object(controller, "handle_mqtt_message", side_effect=record):
            thread = threading.Thread(
                target=controller.on_message, args=(None, None, message)
            )
            thread.start()
            thread.join()
            self.assertEqual([], handled_on)
            await asyncio.sleep(0.01)

        self.assertEqual([threading.get_ident()], handled_on)
        self.assertEqual("manual", controller._mode)


if __name__ == "__main__":
    unittest.main()
