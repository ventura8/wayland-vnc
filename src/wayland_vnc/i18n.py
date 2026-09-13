"""Translation for the settings window.

The window is the part of wayland-vnc a person reads, so it is translated. The CLI's
JSON output deliberately is not: those payloads are a machine-readable contract that
scripts parse, and translating them would break callers.

Languages are listed by endonym -- the name each language uses for itself -- which is
what desktops show and what a speaker recognises when the interface is in a language
they cannot read.

`_` resolves through the active translation on every call rather than binding at import
time, so switching language re-renders the window without restarting the application.
"""

import gettext
import json
import os
from pathlib import Path

DOMAIN = "wayland-vnc"
AUTOMATIC = "auto"
PREFERENCES_NAME = "preferences.json"

# Major languages across every inhabited continent. The endonym is what the settings
# window shows; the code is the gettext catalogue name under share/locale.
LANGUAGES: dict[str, str] = {
    "af": "Afrikaans",
    "am": "አማርኛ",
    "ar": "العربية",
    "bn": "বাংলা",
    "de": "Deutsch",
    "el": "Ελληνικά",
    "es": "Español",
    "fa": "فارسی",
    "fr": "Français",
    "ha": "Hausa",
    "hi": "हिन्दी",
    "id": "Bahasa Indonesia",
    "it": "Italiano",
    "ja": "日本語",
    "ko": "한국어",
    "mi": "Te Reo Māori",
    "nl": "Nederlands",
    "pl": "Polski",
    "pt": "Português",
    "pt_BR": "Português do Brasil",
    "ro": "Română",
    "ru": "Русский",
    "sw": "Kiswahili",
    "th": "ไทย",
    "tr": "Türkçe",
    "uk": "Українська",
    "vi": "Tiếng Việt",
    "yo": "Yorùbá",
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
    "zh": "中文",
    "he": "עברית",
    "ur": "اردو",
    "ps": "پښتو",
    "sd": "سنڌي",
    "yi": "ייִדיש",
    "bg": "Български",
    "cs": "Čeština",
    "da": "Dansk",
    "fi": "Suomi",
    "sv": "Svenska",
    "no": "Norsk bokmål",
    "nn": "Norsk nynorsk",
    "hu": "Magyar",
    "et": "Eesti",
    "eu": "Euskara",
    "gl": "Galego",
    "ca": "Català",
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "sk": "Slovenčina",
    "sl": "Slovenščina",
    "hr": "Hrvatski",
    "sr": "Српски",
    "bs": "Bosanski",
    "mk": "Македонски",
    "sq": "Shqip",
    "mt": "Malti",
    "is": "Íslenska",
    "cy": "Cymraeg",
    "hy": "Հայերեն",
    "ka": "ქართული",
    "kk": "Қазақ тілі",
    "az": "Azərbaycan dili",
    "be": "Беларуская",
    "uz": "Oʻzbekcha",
    "tg": "Тоҷикӣ",
    "tt": "Татарча",
    "ba": "Башҡортса",
    "mn": "Монгол",
    "ms": "Bahasa Melayu",
    "tl": "Tagalog",
    "jv": "Basa Jawa",
    "su": "Basa Sunda",
    "km": "ខ្មែរ",
    "lo": "ລາວ",
    "my": "မြန်မာ",
    "ne": "नेपाली",
    "si": "සිංහල",
    "as": "অসমীয়া",
    "bo": "བོད་སྐད།",
    "br": "Brezhoneg",
    "fo": "Føroyskt",
    "haw": "ʻŌlelo Hawaiʻi",
    "ht": "Kreyòl ayisyen",
    "kn": "ಕನ್ನಡ",
    "la": "Latina",
    "lb": "Lëtzebuergesch",
    "ln": "Lingála",
    "mg": "Malagasy",
    "ml": "മലയാളം",
    "mr": "मराठी",
    "oc": "Occitan",
    "pa": "ਪੰਜਾਬੀ",
    "sa": "संस्कृतम्",
    "sn": "chiShona",
    "so": "Soomaali",
    "ta": "தமிழ்",
    "te": "తెలుగు",
    "tk": "Türkmençe",
    "gu": "ગુજરાતી",
}

# Languages written right to left; the window mirrors itself for these.
RTL_LANGUAGES = frozenset({"ar", "fa", "he", "ur", "ps", "sd", "yi"})

# Held in a dict rather than as rebound module globals so that activating a language
# needs no `global` statement, which the project's lint rules reject outright.
_state: dict = {"catalogue": gettext.NullTranslations(), "language": AUTOMATIC}

# What the desktop session asked for before this process changed anything; Automatic
# hands it back. GTK and libadwaita translate their own strings (the About dialog's
# Credits, Legal, Website, Issue Tracker, the shortcuts window, Close buttons) through
# libc gettext, which reads LANGUAGE on every lookup -- so a language chosen in the
# window is exported there too, and the toolkit's parts follow along wherever the
# system has that language pack installed.
SESSION_LANGUAGE = os.environ.get("LANGUAGE")


def _(message: str) -> str:
    """Translate `message` through whichever catalogue is active right now."""
    return _state["catalogue"].gettext(message)


def translatable(message: str) -> str:
    """Mark a string for extraction without translating it here.

    The runtime raises validation failures as plain English exceptions, which the CLI
    prints verbatim into its JSON and the settings window translates when it displays
    them. Marking them means translators see them; returning them unchanged means the
    machine-readable output does not move.
    """
    return message


def locale_dir() -> Path:
    """Where the compiled catalogues live.

    Installed packages put them under the prefix that owns this module; a source
    checkout keeps them in `build/locale`, which `scripts/build-translations.sh`
    fills. The environment override exists for tests and for relocatable bundles
    (AppImage, Snap, Flatpak) that are mounted at a path decided at run time.
    """
    override = os.environ.get("WAYLAND_VNC_LOCALEDIR")
    if override:
        return Path(override)
    module = Path(__file__).resolve()
    # .../<prefix>/lib/wayland-vnc/wayland_vnc/i18n.py -> <prefix>/share/locale
    for parent in module.parents:
        candidate = parent / "share" / "locale"
        if candidate.is_dir():
            return candidate
    return module.parent.parent.parent / "build" / "locale"


def available(localedir: Path | None = None) -> list[str]:
    """The language codes that actually have a compiled catalogue installed."""
    directory = locale_dir() if localedir is None else localedir
    return sorted(
        code for code in LANGUAGES if (directory / code / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file()
    )


def activate(language: str | None = None, localedir: Path | None = None) -> str:
    """Make `language` the active catalogue and return the code actually in use.

    `AUTOMATIC` (the default) follows the desktop's own locale environment, so the
    window matches the rest of the session without the user choosing anything. An
    unknown or uninstalled language falls back to the untranslated source strings
    rather than failing: a missing catalogue must never stop the window opening.
    """
    requested = AUTOMATIC if language in (None, "", AUTOMATIC) else language
    directory = locale_dir() if localedir is None else localedir
    languages = None if requested == AUTOMATIC else [requested]
    _export_toolkit_language(requested)
    _state["catalogue"] = gettext.translation(
        DOMAIN, localedir=str(directory), languages=languages, fallback=True
    )
    _state["language"] = requested
    return requested


def _export_toolkit_language(requested: str) -> None:
    """Point libc gettext -- hence GTK's and libadwaita's own strings -- at `requested`.

    Automatic restores whatever the session had, including its absence: leaving a
    stale LANGUAGE behind would pin the toolkit to the last explicit choice.
    """
    if requested != AUTOMATIC:
        os.environ["LANGUAGE"] = requested
    elif SESSION_LANGUAGE is None:
        os.environ.pop("LANGUAGE", None)
    else:
        os.environ["LANGUAGE"] = SESSION_LANGUAGE


def active_language() -> str:
    """The language code last passed to `activate`, or `AUTOMATIC`."""
    return _state["language"]


def is_rtl(language: str, env: dict | None = None) -> bool:
    """Whether the interface should be laid out right to left."""
    if language != AUTOMATIC:
        return language.split("_")[0] in RTL_LANGUAGES
    environment = os.environ if env is None else env
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = environment.get(name)
        if value:
            # LANGUAGE is a colon-separated preference list ("ar:en"); its first
            # entry is what gettext will use. The others are single locales, which
            # the same split leaves untouched.
            first = value.split(":")[0]
            return first.split(".")[0].split("_")[0] in RTL_LANGUAGES
    return False


def preferences_path(directory: Path) -> Path:
    """Where the window's own preferences (not the server's config) are kept."""
    return directory / PREFERENCES_NAME


def read_language(directory: Path) -> str:
    """The stored language preference, or `AUTOMATIC` when none is stored.

    A corrupt or unreadable file is treated as no preference: the window must still
    open, in the desktop's own language.
    """
    try:
        stored = json.loads(preferences_path(directory).read_text())
    except (OSError, ValueError):
        return AUTOMATIC
    language = stored.get("language") if isinstance(stored, dict) else None
    return language if language in LANGUAGES or language == AUTOMATIC else AUTOMATIC


def write_language(directory: Path, language: str) -> Path:
    """Store the language preference, leaving any other preferences intact."""
    if language != AUTOMATIC and language not in LANGUAGES:
        raise ValueError(f"Unknown language: {language}")
    path = preferences_path(directory)
    try:
        stored = json.loads(path.read_text())
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    stored["language"] = language
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n")
    return path
