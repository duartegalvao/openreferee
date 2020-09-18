import threading
from functools import wraps

import click
from flask import (
    Blueprint,
    copy_current_request_context,
    current_app,
    json,
    jsonify,
    request,
)
from marshmallow import EXCLUDE
from sqlalchemy.exc import IntegrityError
from webargs.flaskparser import use_kwargs
from werkzeug.exceptions import Conflict, NotFound, Unauthorized

from .app import register_spec
from .db import db
from .defaults import DEFAULT_EDITABLES, SERVICE_INFO
from .models import Event
from .operations import (
    cleanup_event,
    process_editable_files,
    process_revision,
    setup_event_tags,
    setup_file_types,
    setup_requests_session,
)
from .schemas import (
    CreateEditableSchema,
    EventInfoSchema,
    EventSchema,
    ReviewEditableSchema,
)


def require_event_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        identifier = kwargs.pop("identifier")
        event = Event.query.get(identifier)
        if event is None:
            raise NotFound("Unknown event")
        auth = request.headers.get("Authorization")
        token = None
        if auth and auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            raise Unauthorized("Token missing")
        elif token != event.token:
            raise Unauthorized("Invalid token")
        return fn(*args, event=event, **kwargs)

    return wrapper


api = Blueprint("api", __name__, cli_group=None)


@api.route("/info")
def info():
    """Get service info
    ---
    get:
      description: Get service info
      operationId: getServiceInfo
      tags: ["service", "information"]
      responses:
        200:
          description: Service Info
          content:
            application/json:
              schema: ServiceInfoSchema

    """
    return jsonify(SERVICE_INFO)


@api.route("/event/<identifier>", methods=("PUT",))
@use_kwargs(EventSchema, location="json")
def create_event(identifier, title, url, token, endpoints):
    """Create an Event.
    ---
    put:
      description: Create an Event
      operationId: createEvent
      tags: ["event", "create"]
      requestBody:
        content:
          application/json:
            schema: EventSchema
      parameters:
        - in: path
          schema: IdentifierParameter
      responses:
        201:
          description: Event Created
          content:
            application/json:
              schema: SuccessSchema
    """
    event = Event(
        identifier=identifier, title=title, url=url, token=token, endpoints=endpoints,
    )
    db.session.add(event)
    try:
        db.session.flush()
    except IntegrityError:
        raise Conflict("Event already exists")
    current_app.logger.info("Registered event %r", event)

    session = setup_requests_session(token)
    setup_event_tags(session, event)

    response = session.post(
        endpoints["editable_types"], json={"editable_types": list(DEFAULT_EDITABLES)},
    )
    response.raise_for_status()

    setup_file_types(session, event)

    db.session.commit()
    return "", 201


@api.route("/event/<identifier>", methods=("DELETE",))
@require_event_token
def remove_event(event):
    """Remove an Event.
    ---
    delete:
      description: Remove an Event
      operationId: removeEvent
      tags: ["event", "remove"]
      security:
        - bearer_token: []
      parameters:
        - in: path
          schema: IdentifierParameter
      responses:
        204:
          description: Event Removed
          content:
            application/json:
              schema: SuccessSchema
    """
    cleanup_event(event)
    db.session.delete(event)
    db.session.commit()
    current_app.logger.info("Unregistered event %r", event)
    return "", 204


@api.route("/event/<identifier>")
@require_event_token
def get_event_info(event):
    """Get information about an event
    ---
    get:
      description: Get information about an event
      operationId: getEvent
      tags: ["event", "get"]
      security:
        - bearer_token: []
      parameters:
        - in: path
          schema: IdentifierParameter
      responses:
        200:
          description: Event Info
          content:
            application/json:
              schema: EventInfoSchema
    """
    return EventInfoSchema().dump(event)


@api.route(
    "/event/<identifier>/editable/<any(paper,slides,poster):editable_type>/<contrib_id>",
    methods=("PUT",),
)
@use_kwargs(CreateEditableSchema, location="json")
@require_event_token
def create_editable(event, contrib_id, editable_type, editable, revision, endpoints):
    """A new editable is created
    ---
    put:
      description: Called when a new editable is created
      operationId: createEditable
      tags: ["editable", "create"]
      security:
        - bearer_token: []
      requestBody:
        content:
          application/json:
            schema: CreateEditableSchema
      parameters:
        - in: path
          schema: EditableParameters
      responses:
        200:
          description: Editable processed
    """
    current_app.logger.info(
        "A new %r editable was submitted for contribution %r", editable_type, contrib_id
    )
    session = setup_requests_session(event.token)

    @copy_current_request_context
    def watermark_revision_files():
        """Wait until the revision has been committed"""
        response = session.get(endpoints["revisions"]["details"])
        if response.status_code == 200:
            process_editable_files(session, event, revision["files"], endpoints)
            return

        t = threading.Timer(5.0, watermark_revision_files)
        t.daemon = True
        t.start()

    watermark_revision_files()
    return "", 201


@api.route(
    "/event/<identifier>/editable/<any(paper,slides,poster):editable_type>/<contrib_id>/<revision_id>",
    methods=("POST",),
)
@use_kwargs(ReviewEditableSchema(unknown=EXCLUDE), location="json")
@require_event_token
def review_editable(
    event, contrib_id, editable_type, revision_id, action, revision, endpoints
):
    """A new revision is created
    ---
    post:
      description: Called when a new editable is revised
      operationId: reviewEditable
      tags: ["editable", "review"]
      security:
        - bearer_token: []
      requestBody:
        content:
          application/json:
            schema: ReviewEditableSchema
      parameters:
        - in: path
          schema: ReviewParameters
      responses:
        200:
          description: Review processed
          content:
            application/json:
              schema: ReviewResponseSchema
    """
    current_app.logger.info(
        "A new revision %r was submitted for contribution %r", revision_id, contrib_id
    )
    if revision["final_state"]["name"] == "accepted":
        resp = process_revision(event, revision)
        return jsonify(resp), 201
    return "", 201


@api.cli.command("openapi")
@click.option(
    "--json", "as_json", is_flag=True,
)
@click.option(
    "--test", "-t", is_flag=True, help="Specify a test server (useful for Swagger UI)",
)
@click.option("--host", "-h")
@click.option("--port", "-p")
def _openapi(test, as_json, host, port):
    """Generate OpenAPI metadata from Flask app."""
    with current_app.test_request_context():
        spec = register_spec(test=test, test_host=host, test_port=port)
        spec.path(view=info)
        spec.path(view=create_event)
        spec.path(view=remove_event)
        spec.path(view=get_event_info)
        spec.path(view=create_editable)

        if as_json:
            print(json.dumps(spec.to_dict()))
        else:
            print(spec.to_yaml())
