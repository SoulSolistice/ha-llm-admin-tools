"""Bounds that keep a single tool call cheap for Home Assistant and the client."""

from datetime import timedelta

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
MAX_TEXT_FILTER_LENGTH = 200

MAX_ATTRIBUTE_CHARACTERS = 2000
# Longer text is withheld whole: checking it would hold up Home Assistant.
MAX_SCRUBBED_TEXT_CHARACTERS = 100_000
# What is shorter would be removed from everything, leaving nothing readable.
MIN_WITHHELD_TEXT_LENGTH = 2

MAX_HISTORY_ENTITIES = 10
DEFAULT_HISTORY_WINDOW = timedelta(hours=24)
MAX_HISTORY_WINDOW = timedelta(days=7)
# A client rarely knows the time exactly, and its request takes time to arrive. Asking
# for the longest window should not fail over that.
HISTORY_WINDOW_SLACK = timedelta(hours=1)
# Above this many changes, a summary replaces the changes themselves.
MAX_HISTORY_SUMMARY_POINTS = 100
# Fewer statistics rows than this say too little, so states are read instead.
MIN_HISTORY_STATISTICS_ROWS = 20
# The request limit shares itself out, so that many entities still fit in a result.
MAX_HISTORY_POINTS_PER_ENTITY = 1000
MAX_HISTORY_POINTS_PER_REQUEST = 2000
# Reading hundreds of thousands of states delays Home Assistant by seconds.
MAX_HISTORY_STATES_READ_PER_REQUEST = 100_000
MAX_HISTORY_STATES_READ_PER_LOOKBACK = 20_000

MAX_RESULT_CHARACTERS = 200_000
MAX_CHANGE_SUMMARY_CHARACTERS = 500
# How much a report on a removal lists; it states how many there are in all.
MAX_REPORT_ITEMS = 100
MAX_REPORT_USES = 10

MAX_SERVICE_ENTITIES = 50
# In seconds. What takes longer goes on, but the client is told it has not ended.
SERVICE_CALL_TIMEOUT = 30
CORE_COMMAND_TIMEOUT = 30
MAX_JOURNAL_RECORDS = 200
MAX_WRITES_PER_MINUTE = 60
WRITE_RATE_WINDOW_SECONDS = 60

# A client can ask for far more history, if it says why.
MIN_REASON_LENGTH = 10
DEFAULT_EXTENDED_HISTORY_POINTS = 2000
MAX_EXTENDED_HISTORY_POINTS = 10_000
MAX_EXTENDED_STATISTICS_ROWS = 5000
MAX_EXTENDED_RESULT_CHARACTERS = 1_000_000

# The whole log can be asked for too. A log file can be far larger than this.
MAX_FULL_LOG_LINES = 20_000
MAX_FULL_LOG_CHARACTERS = 5_000_000
MAX_LOG_RECORD_CHARACTERS = 4000
# In seconds, as reading it from the Supervisor can take a while.
FULL_LOG_TIMEOUT = 30

# A health report is read before anything else, so it has to stay short.
MAX_HEALTH_ITEMS = 10
MAX_HEALTH_EXAMPLES = 3
MAX_HEALTH_TEXT_CHARACTERS = 300

# Above this, a service is described without the options of its fields.
MAX_SERVICE_DESCRIPTION_CHARACTERS = 4000
