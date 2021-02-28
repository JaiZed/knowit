
from ..property import Property


class Basic(Property):
    """Basic property to handle int, float and other basic types."""

    def __init__(self, name, data_type, allow_fallback=False, **kwargs):
        """Init method."""
        super().__init__(name, **kwargs)
        self.data_type = data_type
        self.allow_fallback = allow_fallback

    def handle(self, value, context):
        """Handle value."""
        if isinstance(value, self.data_type):
            return value

        try:
            return self.data_type(value)
        except ValueError:
            if not self.allow_fallback:
                self.report(value, context)
