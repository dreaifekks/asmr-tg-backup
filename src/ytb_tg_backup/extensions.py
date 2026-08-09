from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
import logging
from pathlib import Path
import threading
import tomllib
from typing import Any

from .config import Config, ExtensionSettings, ExtensionsConfig
from .extension_api import (
    EXTENSION_API_LEVEL,
    EXTENSION_ENTRY_POINT_GROUP,
    ConnectionPolicyFactory,
    ExtensionContext,
    ExtensionError,
    ExtensionManifest,
    ExtensionRegistrar,
    HttpTransportFactory,
    RuntimeFactoryContext,
    SourceAdapterContext,
    SourceProviderDefinition,
)
from .models import Origin
from .network import ConnectionRuntime


@dataclass(frozen=True)
class InstalledExtension:
    id: str
    distribution: str
    version: str
    enabled: bool


@dataclass
class _LoadedExtension:
    id: str
    instance: object
    manifest: ExtensionManifest


class SourceProviderCatalog:
    def __init__(self, definitions: Mapping[str, SourceProviderDefinition]):
        self._definitions = dict(definitions)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def definition(self, provider: str) -> SourceProviderDefinition:
        provider_id = provider.strip().lower()
        try:
            return self._definitions[provider_id]
        except KeyError:
            raise ValueError(f"unsupported source provider: {provider_id!r}") from None

    def kinds(self, provider: str) -> frozenset[str]:
        return self.definition(provider).kinds

    def default_kind(self, provider: str) -> str:
        return self.definition(provider).default_kind

    def validate_origin(self, origin: Origin) -> Origin:
        return self.definition(origin.provider).validate_origin(origin)

    def identity(self, origin: Origin) -> tuple[str, str, str, str]:
        return self.definition(origin.provider).identity(origin)

    def is_live_origin(self, origin: Origin) -> bool:
        return self.definition(origin.provider).is_live_origin(origin)

    def seed_content_kind(self, origin: Origin) -> str | None:
        return self.definition(origin.provider).seed_content_kind(origin)

    def poll_variant(self, origin: Origin) -> str | None:
        return self.definition(origin.provider).poll_variant(origin)

    def route_features(self, provider: str) -> frozenset[str]:
        return self.definition(provider).route_features

    def create_source_registry(
        self,
        *,
        http: ConnectionRuntime,
        logger: logging.Logger,
        data_dir: Path,
    ):
        from .sources import SourceRegistry

        return SourceRegistry(
            tuple(self._definitions.values()),
            SourceAdapterContext(http=http, logger=logger, data_dir=data_dir),
        )


class RuntimeDependencies:
    def __init__(
        self,
        *,
        providers: SourceProviderCatalog,
        connection: ConnectionRuntime,
        host: ExtensionHost,
        data_dir: Path,
        logger: logging.Logger,
    ):
        self.providers = providers
        self.connection = connection
        self.host = host
        self.data_dir = data_dir
        self.logger = logger
        self._start_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self.host.start()
            self._started = True

    def stop(self) -> None:
        with self._start_lock:
            if not self._started:
                return
            self.host.stop()
            self._started = False

    def create_source_registry(self):
        return self.providers.create_source_registry(
            http=self.connection,
            logger=self.logger,
            data_dir=self.data_dir,
        )


class RuntimeBuilder:
    def __init__(self):
        self._source_providers: dict[str, tuple[str, SourceProviderDefinition]] = {}
        self._connection_policy: tuple[str, ConnectionPolicyFactory] | None = None
        self._http_transport: tuple[str, HttpTransportFactory] | None = None
        self._frozen = False

    def scoped(self, owner: str) -> ExtensionRegistrar:
        return _ScopedRegistrar(self, owner)

    def _add_source_provider(
        self,
        owner: str,
        definition: SourceProviderDefinition,
    ) -> None:
        self._check_mutable()
        existing = self._source_providers.get(definition.provider)
        if existing is not None:
            raise ExtensionError(
                f"source provider {definition.provider!r} is already registered "
                f"by {existing[0]!r}"
            )
        self._source_providers[definition.provider] = (owner, definition)

    def _set_connection_policy(
        self,
        owner: str,
        factory: ConnectionPolicyFactory,
    ) -> None:
        self._check_mutable()
        if self._connection_policy is not None:
            raise ExtensionError(
                f"connection policy is already registered by "
                f"{self._connection_policy[0]!r}"
            )
        self._connection_policy = (owner, factory)

    def _set_http_transport(
        self,
        owner: str,
        factory: HttpTransportFactory,
    ) -> None:
        self._check_mutable()
        if self._http_transport is not None:
            raise ExtensionError(
                f"HTTP transport is already registered by {self._http_transport[0]!r}"
            )
        self._http_transport = (owner, factory)

    def build(
        self,
        *,
        host: ExtensionHost,
        data_dir: Path,
        logger: logging.Logger,
    ) -> RuntimeDependencies:
        self._check_mutable()
        if not self._source_providers:
            raise ExtensionError("runtime has no source providers")
        self._frozen = True
        factory_context = RuntimeFactoryContext(data_dir=data_dir, logger=logger)
        policy = (
            self._connection_policy[1](factory_context)
            if self._connection_policy is not None
            else None
        )
        http_transport = (
            self._http_transport[1](factory_context)
            if self._http_transport is not None
            else None
        )
        return RuntimeDependencies(
            providers=SourceProviderCatalog(
                {
                    provider: definition
                    for provider, (_owner, definition) in self._source_providers.items()
                }
            ),
            connection=ConnectionRuntime(policy, http_transport),
            host=host,
            data_dir=data_dir,
            logger=logger,
        )

    def _check_mutable(self) -> None:
        if self._frozen:
            raise ExtensionError("runtime registry is frozen")


class _ScopedRegistrar:
    def __init__(self, builder: RuntimeBuilder, owner: str):
        self._builder = builder
        self._owner = owner

    def add_source_provider(self, definition: SourceProviderDefinition) -> None:
        self._builder._add_source_provider(self._owner, definition)

    def set_connection_policy(self, factory: ConnectionPolicyFactory) -> None:
        self._builder._set_connection_policy(self._owner, factory)

    def set_http_transport(self, factory: HttpTransportFactory) -> None:
        self._builder._set_http_transport(self._owner, factory)


class ExtensionHost:
    def __init__(
        self,
        config: ExtensionsConfig,
        *,
        data_dir: Path,
        logger: logging.Logger,
    ):
        self.config = config
        self.data_dir = data_dir
        self.logger = logger
        self._loaded: list[_LoadedExtension] = []

    def installed(self) -> tuple[InstalledExtension, ...]:
        enabled = set(self.config.enabled)
        result: list[InstalledExtension] = []
        for extension_id, entry_point in sorted(self._entry_points().items()):
            distribution = entry_point.dist
            result.append(
                InstalledExtension(
                    id=extension_id,
                    distribution=(distribution.name if distribution else "<unknown>"),
                    version=(distribution.version if distribution else "<unknown>"),
                    enabled=extension_id in enabled,
                )
            )
        return tuple(result)

    def load_into(self, builder: RuntimeBuilder) -> None:
        entry_points = self._entry_points()
        for extension_id in self.config.enabled:
            settings = self.config.settings[extension_id]
            entry_point = entry_points.get(extension_id)
            if entry_point is None:
                if settings.required:
                    raise ExtensionError(
                        f"required extension {extension_id!r} is not installed"
                    )
                self.logger.warning("optional extension is not installed id=%s", extension_id)
                continue
            try:
                loaded = entry_point.load()
                instance = loaded if hasattr(loaded, "manifest") else loaded()
                manifest = getattr(instance, "manifest", None)
                if not isinstance(manifest, ExtensionManifest):
                    raise ExtensionError(
                        f"extension {extension_id!r} did not expose ExtensionManifest"
                    )
                if manifest.id != extension_id:
                    raise ExtensionError(
                        f"extension entry point {extension_id!r} returned manifest id "
                        f"{manifest.id!r}"
                    )
                if manifest.api_level != EXTENSION_API_LEVEL:
                    raise ExtensionError(
                        f"extension {extension_id!r} API level {manifest.api_level} "
                        f"is incompatible with core API level {EXTENSION_API_LEVEL}"
                    )
                context = ExtensionContext(
                    extension_id=extension_id,
                    config=self._settings_config(settings),
                    data_dir=self.data_dir / "extensions" / extension_id,
                    logger=self.logger.getChild(f"extension.{extension_id}"),
                )
                instance.register(builder.scoped(extension_id), context)
            except ExtensionError:
                raise
            except Exception as exc:
                raise ExtensionError(
                    f"could not load extension {extension_id!r}: {type(exc).__name__}: {exc}"
                ) from exc
            self._loaded.append(
                _LoadedExtension(
                    id=extension_id,
                    instance=instance,
                    manifest=manifest,
                )
            )

    def start(self) -> None:
        started: list[_LoadedExtension] = []
        try:
            for loaded in self._loaded:
                start = getattr(loaded.instance, "start", None)
                if start is not None:
                    start()
                started.append(loaded)
        except Exception as exc:
            for loaded in reversed(started):
                stop = getattr(loaded.instance, "stop", None)
                if stop is not None:
                    try:
                        stop()
                    except Exception:
                        self.logger.exception(
                            "extension cleanup failed id=%s",
                            loaded.id,
                        )
            raise ExtensionError(
                f"could not start extension {loaded.id!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def stop(self) -> None:
        errors: list[str] = []
        for loaded in reversed(self._loaded):
            stop = getattr(loaded.instance, "stop", None)
            if stop is None:
                continue
            try:
                stop()
            except Exception as exc:
                errors.append(f"{loaded.id}: {type(exc).__name__}: {exc}")
                self.logger.exception("extension stop failed id=%s", loaded.id)
        if errors:
            raise ExtensionError("extension shutdown failed: " + "; ".join(errors))

    def _entry_points(self) -> dict[str, metadata.EntryPoint]:
        result: dict[str, metadata.EntryPoint] = {}
        duplicates: set[str] = set()
        for entry_point in metadata.entry_points(
            group=EXTENSION_ENTRY_POINT_GROUP
        ):
            extension_id = entry_point.name.strip().lower()
            if extension_id in result:
                duplicates.add(extension_id)
            result[extension_id] = entry_point
        if duplicates:
            raise ExtensionError(
                "duplicate extension entry point id(s): "
                + ", ".join(sorted(duplicates))
            )
        return result

    @staticmethod
    def _settings_config(settings: ExtensionSettings) -> Mapping[str, Any]:
        file_config: dict[str, Any] = {}
        if settings.config_file is not None:
            try:
                with settings.config_file.open("rb") as file_handle:
                    raw = tomllib.load(file_handle)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ExtensionError(
                    f"cannot load extension config {settings.config_file}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise ExtensionError(
                    f"extension config {settings.config_file} must be a TOML table"
                )
            file_config = raw
        return {**file_config, **settings.options}


def build_runtime(
    config: Config,
    *,
    logger: logging.Logger | None = None,
) -> RuntimeDependencies:
    runtime_logger = logger or logging.getLogger("asmr_tg_backup")
    builder = RuntimeBuilder()
    from .sources import register_builtin_source_providers

    register_builtin_source_providers(builder.scoped("core"), config)
    host = ExtensionHost(
        getattr(config, "extensions", ExtensionsConfig()),
        data_dir=config.app.data_dir,
        logger=runtime_logger,
    )
    host.load_into(builder)
    return builder.build(
        host=host,
        data_dir=config.app.data_dir,
        logger=runtime_logger,
    )
