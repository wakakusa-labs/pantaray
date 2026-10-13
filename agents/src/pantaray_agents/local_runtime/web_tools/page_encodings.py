"""The Python codec that decodes a page whose charset carries a given label.

A label is what a web page declares, not a Python codec name: browsers read it
through the WHATWG Encoding Standard (https://encoding.spec.whatwg.org/), which
sends Shift_JIS to the Windows code page 932, GBK to GB18030, ISO-8859-1 and US-ASCII to
windows-1252, and EUC-KR to the Windows code page 949. Following the same table
reads a page the way the person's browser shows it. A label outside the table,
or one of WHATWG's encodings Python has no codec for (x-user-defined,
replacement), reads as UTF-8 with replacement characters.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# Python codec -> the WHATWG labels of the encoding it decodes.
_LABELS_BY_CODEC: Final = {
    "utf-8": "unicode-1-1-utf-8 unicode11utf8 unicode20utf8 utf-8 utf8 x-unicode20utf8",
    "cp866": "866 cp866 csibm866 ibm866",
    "iso8859-2": "csisolatin2 iso-8859-2 iso-ir-101 iso8859-2 iso88592 iso_8859-2 "
    "iso_8859-2:1987 l2 latin2",
    "iso8859-3": "csisolatin3 iso-8859-3 iso-ir-109 iso8859-3 iso88593 iso_8859-3 "
    "iso_8859-3:1988 l3 latin3",
    "iso8859-4": "csisolatin4 iso-8859-4 iso-ir-110 iso8859-4 iso88594 iso_8859-4 "
    "iso_8859-4:1988 l4 latin4",
    "iso8859-5": "csisolatincyrillic cyrillic iso-8859-5 iso-ir-144 iso8859-5 "
    "iso88595 iso_8859-5 iso_8859-5:1988",
    "iso8859-6": "arabic asmo-708 csiso88596e csiso88596i csisolatinarabic ecma-114 "
    "iso-8859-6 iso-8859-6-e iso-8859-6-i iso-ir-127 iso8859-6 iso88596 "
    "iso_8859-6 iso_8859-6:1987",
    "iso8859-7": "csisolatingreek ecma-118 elot_928 greek greek8 iso-8859-7 "
    "iso-ir-126 iso8859-7 iso88597 iso_8859-7 iso_8859-7:1987 sun_eu_greek",
    # ISO-8859-8 and ISO-8859-8-I differ only in display order, not in bytes.
    "iso8859-8": "csiso88598e csisolatinhebrew hebrew iso-8859-8 iso-8859-8-e "
    "iso-ir-138 iso8859-8 iso88598 iso_8859-8 iso_8859-8:1988 visual "
    "csiso88598i iso-8859-8-i logical",
    "iso8859-10": "csisolatin6 iso-8859-10 iso-ir-157 iso8859-10 iso885910 l6 latin6",
    "iso8859-13": "iso-8859-13 iso8859-13 iso885913",
    "iso8859-14": "iso-8859-14 iso8859-14 iso885914",
    "iso8859-15": "csisolatin9 iso-8859-15 iso8859-15 iso885915 iso_8859-15 l9",
    "iso8859-16": "iso-8859-16",
    "koi8-r": "cskoi8r koi koi8 koi8-r koi8_r",
    "koi8-u": "koi8-ru koi8-u",
    "mac-roman": "csmacintosh mac macintosh x-mac-roman",
    "mac-cyrillic": "x-mac-cyrillic x-mac-ukrainian",
    "cp874": "dos-874 iso-8859-11 iso8859-11 iso885911 tis-620 windows-874",
    "cp1250": "cp1250 windows-1250 x-cp1250",
    "cp1251": "cp1251 windows-1251 x-cp1251",
    "cp1252": "ansi_x3.4-1968 ascii cp1252 cp819 csisolatin1 ibm819 iso-8859-1 "
    "iso-ir-100 iso8859-1 iso88591 iso_8859-1 iso_8859-1:1987 l1 latin1 "
    "us-ascii windows-1252 x-cp1252",
    "cp1253": "cp1253 windows-1253 x-cp1253",
    "cp1254": "cp1254 csisolatin5 iso-8859-9 iso-ir-148 iso8859-9 iso88599 "
    "iso_8859-9 iso_8859-9:1989 l5 latin5 windows-1254 x-cp1254",
    "cp1255": "cp1255 windows-1255 x-cp1255",
    "cp1256": "cp1256 windows-1256 x-cp1256",
    "cp1257": "cp1257 windows-1257 x-cp1257",
    "cp1258": "cp1258 windows-1258 x-cp1258",
    # WHATWG decodes GBK with the gb18030 decoder, four-byte sequences included.
    "gb18030": "chinese csgb2312 csiso58gb231280 gb2312 gb_2312 gb_2312-80 gbk "
    "iso-ir-58 x-gbk gb18030",
    # WHATWG's Big5 includes the Hong Kong extensions.
    "big5hkscs": "big5 big5-hkscs cn-big5 csbig5 x-x-big5",
    "euc_jp": "cseucpkdfmtjapanese euc-jp x-euc-jp",
    "iso2022_jp": "csiso2022jp iso-2022-jp",
    "cp932": "csshiftjis ms932 ms_kanji shift-jis shift_jis sjis windows-31j x-sjis",
    "cp949": "cseuckr csksc56011987 euc-kr iso-ir-149 korean ks_c_5601-1987 "
    "ks_c_5601-1989 ksc5601 ksc_5601 windows-949",
    "utf-16-be": "unicodefffe utf-16be",
    "utf-16-le": "csunicode iso-10646-ucs-2 ucs-2 unicode unicodefeff utf-16 utf-16le",
}
_CODEC_BY_LABEL: Final[Mapping[str, str]] = MappingProxyType(
    {
        label: codec
        for codec, labels in _LABELS_BY_CODEC.items()
        for label in labels.split()
    }
)


def page_codec(label: str | None) -> str:
    """The codec for a declared charset label; UTF-8 when none is declared."""

    if label is None:
        return "utf-8"
    return _CODEC_BY_LABEL.get(label.strip().lower(), "utf-8")


__all__ = ["page_codec"]
