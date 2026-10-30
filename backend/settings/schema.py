"""Small typed settings primitives; only explicitly registered public fields exist."""
from dataclasses import dataclass


class Conflict(ValueError):
    """A valid preference cannot be applied in the current deployment state."""


@dataclass(frozen=True)
class Field:
    key: str
    default: object
    choices: tuple = ()
    minimum: int | None = None
    maximum: int | None = None
    message: str = 'Choose a supported setting value.'

    def validate(self, value):
        if type(value) is not type(self.default):
            raise ValueError(self.message)
        if self.choices and value not in self.choices:
            raise ValueError(self.message)
        if ((self.minimum is not None and value < self.minimum) or
                (self.maximum is not None and value > self.maximum)):
            raise ValueError(self.message)
        return str(value).lower() if isinstance(value, bool) else str(value)

    def read(self, store):
        raw = store.get(self.key, str(self.default).lower())
        try:
            if isinstance(self.default, bool):
                if raw not in ('true', 'false'): return self.default
                value = raw == 'true'
            elif isinstance(self.default, int): value = int(raw)
            else: value = raw
            self.validate(value)
            return value
        except (ValueError, TypeError):
            return self.default


def validate(fields, values):
    if not isinstance(values, dict) or not values or set(values) - fields.keys():
        raise ValueError('Choose a supported setting.')
    return {fields[name].key: fields[name].validate(value) for name, value in values.items()}


def write(db, values):
    db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', values.items())
