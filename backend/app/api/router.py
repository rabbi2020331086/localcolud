from fastapi import APIRouter

from app.api.routes.admin import router as admin
from app.api.routes.amis import router as amis
from app.api.routes.auth import router as auth
from app.api.routes.ec2_resources import key_pairs, security_groups, volumes
from app.api.routes.instances import router as instances
from app.api.routes.load_balancers import router as load_balancers
from app.api.routes.nodes import agent as node_agent
from app.api.routes.nodes import agent_files as node_agent_files
from app.api.routes.nodes import router as nodes
from app.api.routes.resources import groups, keys, operations, templates

router = APIRouter()
for child in (
    auth,
    admin,
    amis,
    instances,
    operations,
    keys,
    templates,
    groups,
    load_balancers,
    nodes,
    node_agent,
    node_agent_files,
    key_pairs,
    security_groups,
    volumes,
):
    router.include_router(child)
