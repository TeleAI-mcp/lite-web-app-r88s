# This file is part of the Flask web framework
# Copyright (C) 2010-2024 by Armin Ronacher and contributors.
# See AUTHORS for more details.
# All rights reserved.

"""
    flask.app
    ~~~~~~~~~

    This module implements the central WSGI application object.

    :copyright: (c) 2010 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""

import datetime
import os
import sys
import typing as t
from threading import Lock
from types import TracebackType
from urllib.parse import urljoin

from werkzeug.exceptions import abort as werkzeug_abort
from werkzeug.exceptions import HTTPException as WerkzeugHTTPException
from werkzeug.exceptions import InternalServerError
from werkzeug.local import LocalProxy
from werkzeug.routing import BuildError
from werkzeug.serving import is_running_from_reloader
from werkzeug.wrappers import Request as RequestBase
from werkzeug.wrappers import Response as ResponseBase

from . import cli
from .config import Config
from .ctx import AppContext, RequestContext
from .globals import _app_ctx_stack, _request_ctx_stack
from .helpers import (
    _endpoint_from_view_func,
    find_package,
    get_debug_flag,
    get_flashed_messages,
    url_for,
)
from .json import JSONDecoder as _JSONDecoder
from .json import JSONEncoder as _JSONEncoder
from .json.tag import TaggedJSONSerializer
from .logging import create_logger
from .sessions import SecureCookieSessionInterface
from .signals import appcontext_tearing_down, request_finished, request_started
from .templating import DispatchingJinjaLoader, Environment
from .typing import AfterRequestCallable, BeforeFirstRequestCallable, BeforeRequestCallable, ErrorHandlerCallable, TeardownCallable
from .wrappers import Request, Response

if t.TYPE_CHECKING:
    from .testing import FlaskClient

# The sentinel value for the default parameters
def_sentinel = object()


def _make_timedelta(value: t.Optional[t.Union[int, float, datetime.timedelta]]) -> t.Optional[datetime.timedelta]:
    """Create a timedelta from a value."""
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return value
    return datetime.timedelta(seconds=value)


def _get_exc_class_name(exc_class: t.Type[Exception]) -> str:
    """Get the name of an exception class."""
    return exc_class.__module__ + "." + exc_class.__name__


def _find_package_path(import_name: str) -> str:
    """Find the path to a package."""
    root_module_name = import_name.split(".")[0]
    loader = sys.modules.get(root_module_name).__loader__
    if loader is None:
        raise RuntimeError(
            f"No loader found for module {root_module_name!r}. "
            "Make sure the module is installed."
        )
    if hasattr(loader, "get_filename"):
        filepath = loader.get_filename(root_module_name)  # type: ignore
    else:
        raise RuntimeError(
            f"The loader for module {root_module_name!r} does not have a get_filename method."
        )
    return os.path.dirname(os.path.abspath(filepath))


class Flask:
    """The Flask object implements a WSGI application and acts as the central
    object.  It is passed the name of the module or package of the
    application.  Once it is created it will act as a central registry for
    the view functions, the URL rules, template configuration and much more.

    The name of the package is used to resolve resources from inside the
    package or the folder the module is contained in depending on if the
    package parameter resolves to an actual python package (a folder with
    an :file:`__init__.py` file inside) or a standard module (just a ``.py`` file).

    For more information about resource loading, see :func:`open_resource`.

    Usually you create a :class:`Flask` instance in your main module or
    in the :file:`__init__.py` file of your package like this::

        from flask import Flask
        app = Flask(__name__)

    .. admonition:: About the First Parameter

        The idea of the first parameter is to give Flask an idea of what
        belongs to your application.  This name is used to find resources
        on the filesystem, can be used by extensions to improve debugging
        information and a lot more.

        So it's important what you provide there.  If you are using a single
        module, `__name__` is always the correct value.  If you however are
        using a package, it's usually recommended to hardcode the name of
        your package there.

        For example if your application is defined in ``yourapplication/app.py``
        you should create it with one of the two versions below::

            from flask import Flask
            app = Flask('yourapplication')
            from yourapplication import app

        If you are using a package, ``__name__`` will be the name of the
        package, which is also correct. So you can also do::

            from flask import Flask
            app = Flask(__name__)

    .. versionchanged:: 1.0
        The ``static_host`` parameter was added.

    .. versionchanged:: 1.0
        The ``host_matching`` and ``static_host`` parameters were added.

    .. versionchanged:: 1.0
        The ``subdomain_matching`` parameter was added.

    .. versionchanged:: 0.7
        The ``static_url_path``, ``static_folder``, and ``template_folder"
        parameters were added.

    .. versionchanged:: 0.5
        The ``instance_relative_config`` parameter was added.

    :param import_name: the name of the application package
    :param static_url_path: can be used to specify a different path for the
                            static files on the web.  Defaults to the name
                            of the `static_folder` folder.
    :param static_folder: the folder with static files that should be served
                          at `static_url_path`.  Defaults to the ``'static'``
                          folder in the root path of the application.
    :param static_host: the host to use when adding the static route.
                         Defaults to None.
    :param host_matching: set ``True`` if the application should handle
                          subdomain matching.  Defaults to ``False``.
    :param subdomain_matching: set ``True`` if the application should handle
                               subdomain matching.  Defaults to ``False``.
    :param instance_path: An alternative instance path for the application.
                          By default the folder ``'instance'`` next to the
                          package or module is assumed to be the instance
                          path.
    :param instance_relative_config: if set to ``True`` relative configuration
                                     filenames are assumed to be relative to
                                     the instance path.
    :param root_path: The path to the root of the application. By default
                      this is the directory containing the package or module.
    """

    #: The class that is used for request objects.  See :class:`~flask.Request`
    #: for more information.
    request_class = Request

    #: The class that is used for response objects.  See :class:`~flask.Response`
    #: for more information.
    response_class = Response

    #: The class that is used for the JSON encoder.  See :class:`~flask.json.JSONEncoder`
    #: for more information.
    json_encoder = _JSONEncoder

    #: The class that is used for the JSON decoder.  See :class:`~flask.json.JSONDecoder`
    #: for more information.
    json_decoder = _JSONDecoder

    #: The class that is used for the jinja environment.
    jinja_environment = Environment

    #: The class that is used for the jinja loader.
    jinja_loader = DispatchingJinjaLoader

    #: The class that is used for the session interface.
    session_interface = SecureCookieSessionInterface()

    def __init__(
        self,
        import_name: str,
        static_url_path: t.Optional[str] = None,
        static_folder: t.Optional[t.Union[str, os.PathLike]] = "static",
        static_host: t.Optional[str] = None,
        host_matching: bool = False,
        subdomain_matching: bool = False,
        instance_path: t.Optional[str] = None,
        instance_relative_config: bool = False,
        root_path: t.Optional[str] = None,
    ) -> None:
        self._got_first_request = False
        self._before_request_lock = Lock()
        self._after_request_lock = Lock()
        self._before_first_request_lock = Lock()

        # Set the name of the application
        self.name = import_name

        # Set the root path
        if root_path is None:
            root_path = _get_package_path(self.name)
        self.root_path = root_path

        # Set the instance path
        if instance_path is None:
            instance_path = self.auto_find_instance_path()
        elif not os.path.isabs(instance_path):
            raise ValueError(
                "If an instance path is provided it must be either "
                "None or an absolute path to an existing directory."
            )
        self.instance_path = instance_path

        # Set the config
        self.config = Config(self.root_path, self)
        self.config["APPLICATION_ROOT"] = "/"
        self.config["SESSION_COOKIE_NAME"] = "session"
        self.config["SESSION_COOKIE_DOMAIN"] = None
        self.config["SESSION_COOKIE_PATH"] = None
        self.config["SESSION_COOKIE_HTTPONLY"] = True
        self.config["SESSION_COOKIE_SECURE"] = False
        self.config["SESSION_COOKIE_SAMESITE"] = None
        self.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=31)
        self.config["SEND_FILE_MAX_AGE_DEFAULT"] = None
        self.config["TRAP_HTTP_EXCEPTIONS"] = False
        self.config["TRAP_BAD_REQUEST_ERRORS"] = False
        self.config["PREFERRED_URL_SCHEME"] = "http"
        self.config["JSON_AS_ASCII"] = True
        self.config["JSON_SORT_KEYS"] = True
        self.config["JSONIFY_PRETTYPRINT_REGULAR"] = True
        self.config["JSONIFY_MIMETYPE"] = "application/json"
        self.config["TEMPLATES_AUTO_RELOAD"] = None
        self.config["EXPLAIN_TEMPLATE_LOADING"] = False
        self.config["MAX_COOKIE_SIZE"] = 4093

        # Set the instance relative config flag
        self.instance_relative_config = instance_relative_config

        # Set the static folder and static URL path
        if static_folder is not None:
            self._static_folder = os.fspath(static_folder)  # type: ignore
        else:
            self._static_folder = None  # type: ignore

        if static_url_path is not None:
            self._static_url_path = static_url_path
        elif self._static_folder is not None:
            self._static_url_path = static_url_path
        else:
            self._static_url_path = None

        self.static_host = static_host
        self.host_matching = host_matching
        self.subdomain_matching = subdomain_matching

        # Set the view functions dict
        self.view_functions: t.Dict[str, t.Callable] = {}

        # Set the error handlers dict
        self.error_handler_spec: t.Dict[
            t.Optional[int], t.Dict[t.Type[Exception], t.Callable]
        ] = {}

        # Set the before request functions list
        self.before_request_funcs: t.Dict[t.Optional[int], t.List[BeforeRequestCallable]] = (
            {}
        )

        # Set the after request functions list
        self.after_request_funcs: t.Dict[t.Optional[int], t.List[AfterRequestCallable]] = {}

        # Set the before first request functions list
        self.before_first_request_funcs: t.List[BeforeFirstRequestCallable] = []

        # Set the teardown functions list
        self.teardown_appcontext_funcs: t.List[TeardownCallable] = []
        self.teardown_request_funcs: t.Dict[t.Optional[int], t.List[TeardownCallable]] = (
            {}
        )

        # Set the URL map
        self.url_map = None
        self.url_map_class = None
        self.url_rule_class = None
        self.url_adapter_class = None

        # Set the template context processors list
        self.template_context_processors: t.Dict[
            t.Optional[int], t.List[t.Callable[[], t.Dict[str, t.Any]]]
        ] = {}

        # Set the shell context processors list
        self.shell_context_processors: t.List[t.Callable[[], t.Dict[str, t.Any]]] = []

        # Set the blueprints dict
        self.blueprints: t.Dict[str, "Blueprint"] = {}

        # Set the extensions dict
        self.extensions: t.Dict[str, t.Any] = {}

        # Set the logger
        self.logger = None
        self._logger_lock = Lock()

        # Initialize the application
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Setup logging for the application."""
        if self.logger is not None:
            return
        self.logger = create_logger(self)

    def auto_find_instance_path(self) -> str:
        """Try to automatically locate the instance path.

        By default the instance folder is assumed to be located next to
        the package or module. If the package is a zip file or an egg
        file, the instance folder is assumed to be located next to the
        zip file or egg file.

        :return: the instance path
        """
        prefix, package_path = find_package(self.import_name)
        if prefix is None:
            return os.path.join(package_path, "instance")
        return os.path.join(prefix, "var", self.name + "-instance")

    @property
    def import_name(self) -> str:
        """The name of the package or module that this app belongs to."""
        return self.name

    @property
    def static_folder(self) -> t.Optional[str]:
        """The absolute path to the configured static folder."""
        if self._static_folder is not None:
            return os.path.join(self.root_path, self._static_folder)
        return None

    @property
    def static_url_path(self) -> t.Optional[str]:
        """The URL prefix that the static files will be registered under."""
        return self._static_url_path

    @property
    def has_static_folder(self) -> bool:
        """Whether the application has a static folder."""
        return self.static_folder is not None

    def run(
        self,
        host: t.Optional[str] = None,
        port: t.Optional[int] = None,
        debug: t.Optional[bool] = None,
        load_dotenv: bool = True,
        **options: t.Any,
    ) -> None:
        """Runs the application on a local development server.

        Do not use ``run()`` in a production setting. It is not intended to
        meet security and performance requirements for a production server.
        Instead, see :doc:`/deploying` for WSGI server recommendations.

        If the ``debug`` flag is set the server will automatically reload
        for code changes and show a debugger in case an exception happens.

        This functionality is enabled if ``debug`` is set or ``True``.

        .. versionchanged:: 1.0
            If port is not specified, use 5000.

        .. versionchanged:: 0.10
            If port is not specified, use 5000.

        :param host: the hostname to listen on. Set this to ``'0.0.0.0'`` to
            have the server available externally as well. Defaults to
            ``'127.0.0.1'`` or the host in the ``SERVER_NAME`` config variable
            if present.
        :param port: the port of the webserver. Defaults to ``5000`` or the
            port defined in the ``SERVER_NAME`` config variable if present.
        :param debug: if given, enable or disable debug mode. See
            :attr:`debug`.
        :param load_dotenv: Load the nearest :file:`.env` and :file:`.flaskenv`
            files to set environment variables. Will also change the working
            directory to be the location of those files.
        :param options: the options to be forwarded to the underlying Werkzeug
            server. See :func:`werkzeug.serving.run_simple` for more
            information.
        """
        from werkzeug.serving import run_simple

        if os.environ.get("FLASK_RUN_FROM_CLI") == "true":
            if not is_running_from_reloader():
                click.echo(
                    "\n  Do not use the development server in a production environment.\n"
                    "  Use a production WSGI server instead.\n",
                    err=True,
                )

        # Set the host and port
        if host is None:
            host = "127.0.0.1"
        if port is None:
            server_name = self.config.get("SERVER_NAME")
            if server_name:
                port = int(server_name.rsplit(":", 1)[-1])
            else:
                port = 5000

        # Set the debug flag
        if debug is None:
            debug = get_debug_flag()

        # Set the options
        options.setdefault("use_reloader", debug)
        options.setdefault("use_debugger", debug)
        options.setdefault("threaded", True)

        # Run the server
        cli.show_server_banner(self, host, port, debug)
        run_simple(host, port, self, **options)

    def test_client(self) -> "FlaskClient":
        """Creates a test client for this application.

        For information about unit testing head over to :doc:`/testing`.

        :return: a test client
        """
        from .testing import FlaskClient

        return FlaskClient(self)

    def open_session(self, request: Request) -> t.Optional[SessionMixin]:
        """Create or open a new session.

        :param request: an instance of :attr:`request_class`
        """
        return self.session_interface.open_session(self, request)

    def save_session(self, session: t.Optional[SessionMixin], response: Response) -> None:
        """Save the session if it needs updates.

        :param session: the session to be saved (a
            :class:`~flask.sessions.SessionMixin` object)
        :param response: an instance of :attr:`response_class`
        """
        return self.session_interface.save_session(self, session, response)
