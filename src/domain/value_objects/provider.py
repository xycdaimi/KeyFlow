from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Provider:
    name: str

    def __post_init__(self) -> None:
        normalized = self.name.strip().lower()
        if not normalized:
            raise ValueError("provider name cannot be empty")
        object.__setattr__(self, "name", normalized)

    def __str__(self) -> str:
        return self.name
