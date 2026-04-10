"""Constants for the Fresh Intellivent Sky integration."""

DOMAIN = "fresh_intellivent_sky_alt"

NAME = ["Intellivent SKY", "Intellivent ICE"]

DISPATCH_DETECTION = f"{DOMAIN}.detection"

DEFAULT_SCAN_INTERVAL = 120
DEFAULT_MODE_FETCH_EVERY = 3
DEFAULT_DEVICE_INFO_FETCH_EVERY = 30
DEFAULT_WRITE_DEBOUNCE_MS = 500
DEFAULT_ERROR_COOLDOWN_SECONDS = 30
TIMEOUT = 30.0

AUTH_MANUAL = "auth_manual"
AUTH_FETCH = "auth_fetch"
NO_AUTH = "no_auth"
AUTH_CODE_ONLY_ZERO = "auth_code_only_zero"
AUTH_CODE_EMPTY = "auth_code_empty"

CONF_AUTH_KEY = "auth_key"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MODE_FETCH_EVERY = "mode_fetch_every"
CONF_DEVICE_INFO_FETCH_EVERY = "device_info_fetch_every"
CONF_WRITE_DEBOUNCE_MS = "write_debounce_ms"
CONF_ERROR_COOLDOWN_SECONDS = "error_cooldown_seconds"

DETECTION_OFF = "Off"

BOOST_UPDATE = "boost_update"
CONSTANT_SPEED_UPDATE = "constant_speed_update"
PAUSE_UPDATE = "pause_update"

AIRING_MODE_UPDATE = "airing_update"
HUMIDITY_MODE_UPDATE = "humidity_mode_update"
LIGHT_AND_VOC_MODE_UPDATE = "light_and_voc_mode_update"
TIMER_MODE_UPDATE = "timer_mode_update"

DETECTION_KEY = "detection"
ENABLED_KEY = "enabled"
DELAY_KEY = "delay"
RPM_KEY = "rpm"
MINUTES_KEY = "minutes"
SECONDS_KEY = "seconds"
