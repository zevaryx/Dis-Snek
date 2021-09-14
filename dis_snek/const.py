"""
Constants used throughout Snek.

:__version__: The version of the library.
:__repo_url__: The URL of the repository.
:__py_version__: The python version in use.
:logger_name: The logger name used by Snek.
:kwarg_spam: Should ``unused kwargs`` be logged.

:GLOBAL_SCOPE: Represents a global scope.
:ACTION_ROW_MAX_ITEMS: The maximum number of items in an action row.
:SELECTS_MAX_OPTIONS: The maximum number of options a select may have.
:SELECT_MAX_NAME_LENGTH: The max length of a select's name.

:CONTEXT_MENU_NAME_LENGTH: The max length of a context menu's name.
:SLASH_CMD_NAME_LENGTH: The max legnth of a slash command's name.
:SLASH_CMD_MAX_DESC_LENGTH: The maximum length of a slash command's description.
:SLASH_CMD_MAX_OPTIONS: The maximum number of options a slash command may have.
:SLASH_OPTION_NAME_LENGTH: The maximum length of a slash option's name.
"""

import sys


_ver_info = sys.version_info


__version__ = "0.0.0"
__repo_url__ = "https://github.com/LordOfPolls/dis_snek"
__py_version__ = f"{_ver_info[0]}.{_ver_info[1]}"
logger_name = "dis.snek"
kwarg_spam = False

GLOBAL_SCOPE = 0
ACTION_ROW_MAX_ITEMS = 5
SELECTS_MAX_OPTIONS = 25
SELECT_MAX_NAME_LENGTH = 100

CONTEXT_MENU_NAME_LENGTH = 32
SLASH_CMD_NAME_LENGTH = 32
SLASH_CMD_MAX_DESC_LENGTH = 100
SLASH_CMD_MAX_OPTIONS = 25
SLASH_OPTION_NAME_LENGTH = 100
