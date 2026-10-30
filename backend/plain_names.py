"""Plain text for new media names; stored paths are never migrated here."""
import re
import unicodedata


def plain_text(value):
    value = str(value or '').translate(str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"', '–': '-', '—': '-', '…': '...', 'ß': 'ss', 'æ': 'ae', 'Æ': 'AE', 'ø': 'o', 'Ø': 'O', 'ł': 'l', 'Ł': 'L', 'œ': 'oe'}))
    value = re.sub('[\U0001f1e6-\U0001faff\u2600-\u27bf\ufe0f\u200d\u20e3]', '', value)
    return unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
