from pathlib import Path

import io
import logging
import os
import tarfile
import threading
from concurrent.futures.thread import ThreadPoolExecutor
from configparser import Error as ConfigParserError

from kivy.config import ConfigParser
from kivy.logger import Logger as logger

from oscpy.server import ServerClass
from waclient.common_config import (
    APP_CONFIG_FILE,
    INTERNAL_KEYS_DIR,
    EXTERNAL_DATA_EXPORTS_DIR,
    get_encryption_conf,
    IS_ANDROID, WIP_RECORDING_MARKER, CONTEXT)
from waclient.recording_toolchain import (
    build_recording_toolchain,
    start_recording_toolchain,
    stop_recording_toolchain,
)
from waclient.utilities.logging import CallbackHandler
from waclient.utilities.misc import safe_catch_unhandled_exception
from waclient.utilities.osc import get_osc_server, get_osc_client
from wacryptolib.container import decrypt_data_from_container, load_container_from_filesystem
from wacryptolib.key_storage import FilesystemKeyStorage, FilesystemKeyStoragePool
from wacryptolib.utilities import load_from_json_file

# os.environ["KIVY_NO_CONSOLELOG"] = "1"  # IMPORTANT

osc, osc_starter_callback = get_osc_server(is_master=False)

# FIXME what happens if exception on remote OSC endpoint ? CRASH!!
# TODO add custom "local escrow resolver"
# TODO add exception swallowers, and logging pushed to frontend app (if present)


THREAD_POOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="service_worker"  # SINGLE worker for now, to avoid concurrency
)

if IS_ANDROID:
    from waclient.android_utils import preload_java_classes
    preload_java_classes()


@ServerClass
class BackgroundServer(object):

    """
    The background server automatically starts when service script is launched.

    It must be stopped gracefully with a call to "/stop_server", so that current recordings can be properly stored.

    While the server is alive, recordings can be started and stopped several times without problem.
    """

    _sock = None

    _recording_toolchain = None

    _status_change_in_progress = False  # Set to True while recording is starting/stopping

    def __init__(self):
        logger.info("Starting service")  # Will not be sent to App (too early)
        osc_starter_callback()  # Opens server port
        self._osc_client = get_osc_client(to_master=True)
        logging.getLogger(None).addHandler(
            CallbackHandler(self._remote_logging_callback)
        )
        self._termination_event = threading.Event()
        self._key_storage_pool = FilesystemKeyStoragePool(INTERNAL_KEYS_DIR)
        logger.info("Service started")

        # Initial setup of service according to persisted config
        config = self._load_config()
        daemonize_service = config.getboolean("usersettings", "daemonize_service")
        self.switch_daemonize_service(daemonize_service)
        if WIP_RECORDING_MARKER.exists():
            self.start_recording()  # Autorecord e.g. after a restart due to closing of main android Activity

    def _remote_logging_callback(self, msg):
        return self._send_message("/log_output", "Service: " + msg)

    def _send_message(self, address, *values):
        #print("Message sent from service to app: %s" % address)
        try:
            return self._osc_client.send_message(address, values=values)
        except OSError as exc:
            # NO LOGGING HERE, else it would loop due to custom logging handler
            print(
                "{SERVICE} Could not send osc message %s%s to app: %r"
                % (address, values, exc)
            )
            return

    def _offload_task(self, method, *args, **kwargs):
        return THREAD_POOL_EXECUTOR.submit(method, *args, **kwargs)

    def _load_config(self, filename=APP_CONFIG_FILE):
        logger.info(f"Reloading config file {filename}")
        config = (
            ConfigParser()
        )  # No NAME here, sicne named parsers must be Singletons in process!
        try:
            if not os.path.exists(filename):
                raise FileNotFoundError(filename)
            config.read(str(filename))  # Fails silently if file not found
        except ConfigParserError as exc:
            logger.error(
                f"Service: Ignored missing or corrupted config file {filename}, ignored ({exc!r})"
            )
            raise
        # logger.info(f"Config file {filename} loaded")
        return config

    @osc.address_method("/ping")
    @safe_catch_unhandled_exception
    def ping(self):
        logger.info("Ping successful!")
        self._send_message("/log_output", "Pong")

    @safe_catch_unhandled_exception
    def _offloaded_switch_daemonize_service(self, value):
        value = bool(value)  # Normalize from possible integer
        logger.info("Switching service persistence to %s", value)
        if IS_ANDROID:
            from jnius import autoclass
            PythonService = autoclass('org.kivy.android.PythonService')
            PythonService.mService.setAutoStopService(not value)
        # Nothing to do for desktop platforms

    @osc.address_method("/switch_daemonize_service")
    @safe_catch_unhandled_exception
    def switch_daemonize_service(self, value):
        return self._offload_task(self._offloaded_switch_daemonize_service, value=value)

    @safe_catch_unhandled_exception
    def _offloaded_start_recording(self, env):
        try:
            encryption_conf = get_encryption_conf(env)
            if self.is_recording:
                #logger.debug("Ignoring redundant call to service.start_recording()")
                return
            logger.info("Starting recording")
            if not self._recording_toolchain:
                config = self._load_config()
                self._recording_toolchain = build_recording_toolchain(
                    config,
                        key_storage_pool=self._key_storage_pool,
                    encryption_conf=encryption_conf,
                )
            if self._recording_toolchain:  # Else we just let cancellation occur
                start_recording_toolchain(self._recording_toolchain)
                logger.info("Recording started")

                if IS_ANDROID:
                    from waclient.android_utils import build_notification_channel, build_notification
                    build_notification_channel(CONTEXT, "Witness Angel Service")
                    notification = build_notification(CONTEXT, title="Sensors are active", message="Click to manage Witness Angel state",
                                                      ticker="Witness Angel sensors are active")
                    notification_uid = 1
                    CONTEXT.startForeground(notification_uid, notification)

        finally:
            self._status_change_in_progress = False
            self.broadcast_recording_state()  # Even on error

    @osc.address_method("/start_recording")
    @safe_catch_unhandled_exception
    def start_recording(self, env=None):
        self._status_change_in_progress = True
        return self._offload_task(self._offloaded_start_recording, env=env)

    @property
    def is_recording(self):
        return bool(
            self._recording_toolchain
            and self._recording_toolchain["sensors_manager"].is_running
        )

    @osc.address_method("/broadcast_recording_state")
    @safe_catch_unhandled_exception
    def broadcast_recording_state(self):
        """
        Broadcasts a TERNARY state, with the special value "" if a status change is in progress
        (since OSC doesn't like None values...)
        """
        if self._status_change_in_progress:
            is_recording = ""
        else:
            is_recording = self.is_recording
        #logger.debug("Broadcasting service state (is_recording=%r)" % is_recording)
        self._send_message("/receive_recording_state", is_recording)

    @safe_catch_unhandled_exception
    def _offloaded_stop_recording(self):
        try:
            if not self.is_recording:
                #logger.debug("Ignoring redundant call to service.stop_recording()")
                return
            logger.info("Stopping recording")
            stop_recording_toolchain(self._recording_toolchain)
            logger.info("Recording stopped")

            if IS_ANDROID:
                CONTEXT.stopForeground(True)  # Does remove notification

        finally:  # Trigger all this even if container flushing failed
            self._recording_toolchain = (
                None
            )  # Will force a reload of config on next recording
            self._status_change_in_progress = False
            self.broadcast_recording_state()

    @osc.address_method("/stop_recording")
    @safe_catch_unhandled_exception
    def stop_recording(self):
        self._status_change_in_progress = True
        return self._offload_task(self._offloaded_stop_recording)

    @safe_catch_unhandled_exception
    def _offloaded_attempt_container_decryption(self, container_filepath):
        logger.info("Decryption requested for container %s", container_filepath)
        target_directory = EXTERNAL_DATA_EXPORTS_DIR.joinpath(
            os.path.basename(container_filepath)
        )
        target_directory.mkdir(
            exist_ok=True
        )  # Double exports would replace colliding files
        container = load_container_from_filesystem(container_filepath, include_data_ciphertext=True)
        tarfile_bytes = decrypt_data_from_container(
            container, key_storage_pool=self._key_storage_pool
        )
        tarfile_bytesio = io.BytesIO(tarfile_bytes)
        tarfile_obj = tarfile.open(
            mode="r", fileobj=tarfile_bytesio  # TODO add gzip support here one day
        )
        # Beware, as root on unix systems it would apply chown/chmod
        tarfile_obj.extractall(target_directory)
        logger.info(
            "Container content was successfully decrypted into folder %s",
            target_directory,
        )

    @osc.address_method("/attempt_container_decryption")
    @safe_catch_unhandled_exception
    def attempt_container_decryption(self, container_filepath: str):
        container_filepath = Path(container_filepath)
        return self._offload_task(self._offloaded_attempt_container_decryption, container_filepath=container_filepath)

    @osc.address_method("/stop_server")
    @safe_catch_unhandled_exception
    def stop_server(self):
        logger.info("Stopping service")

        if self.is_recording:
            logger.info(
                "Recording is in progress, we stop it as part of service shutdown"
            )
            self.stop_recording().result(timeout=30)   # SYNCHRONOUS CALL (but through threadpool still)

        osc.stop_all()
        self._termination_event.set()
        logger.info("Service stopped")

    @safe_catch_unhandled_exception
    def join(self):
        """
        Wait for the termination of the background server
        (meant for use by the main thread of the service process).
        """
        self._termination_event.wait()


def main():
    logger.info("Service process launches")
    server = BackgroundServer()
    server.join()
    logger.info("Service process exits")
