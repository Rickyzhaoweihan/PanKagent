"""Project saved or freshly planned routes to Cytoscape quadratic segments."""
import json
import math

from pankagent_vnext.transport import RESPONSE_BYTE_LIMIT


def point(value):
    return (isinstance(value, (tuple, list)) and len(value) == 2
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in value))


def towards(a, b, fraction):
    return [x + (y - x) * fraction for x, y in zip(a, b)]


def controls(route):
    start, end = route.get("source_port"), route.get("target_port")
    if not point(start) or not point(end) or start == end:
        return None
    if route.get("route_type") == "bezier":
        cp = route.get("control_points")
        if isinstance(cp, list) and len(cp) == 2 and all(map(point, cp)):
            # Two quadratics match cubic end tangents and its exact midpoint.
            return [towards(start, cp[0], .75), towards(end, cp[1], .75)]
        return None
    if route.get("route_type") != "polyline":
        return None
    corners = route.get("waypoints", [])
    if not isinstance(corners, list) or not all(map(point, corners)):
        return None
    vertices = []
    for p in [start, *corners, end]:
        if vertices and math.dist(vertices[-1], p) < .001:
            continue
        while len(vertices) >= 2:
            a, b = vertices[-2:]
            if math.dist(a, b) + math.dist(b, p) - math.dist(a, p) > .001:
                break
            vertices.pop()
        vertices.append(p)
    result = []
    for a, b, c in zip(vertices, vertices[1:], vertices[2:]):
        incoming, outgoing = math.dist(a, b), math.dist(b, c)
        reach = min(24., incoming * .45, outgoing * .45)
        # Midpoints between adjacent controls lie on the reserved straight lanes.
        result.extend([towards(b, a, reach / incoming), b, towards(b, c, reach / outgoing)])
    return result or [towards(start, end, .5)]


def spread_hub_routes(data, routes):
    """Reserve a readable band for each group of spokes in a single-hub layout."""
    if (data.get("layout") or {}).get("engine") != "center_spread":
        return
    positions = data.get("xy_json", {})
    edges = data.get("combined_query_result", {}).get("edges", [])
    if not edges:
        return
    pairs = [(e.get("~start"), e.get("~end")) for e in edges]
    common = set.intersection(*(set(p) for p in pairs))
    if len(common) != 1 or any(a == b for a, b in pairs):
        return
    hub = common.pop()
    groups = {}
    for e, (a, b) in zip(edges, pairs):
        leaf = b if a == hub else a
        if (leaf not in positions or hub not in positions
                or not isinstance(routes.get(e.get("~id")), dict)):
            return
        groups.setdefault(leaf, []).append(e)
    centre = positions[hub]
    for side in (-1, 1):
        leaves = sorted((n for n in groups if (positions[n]["x"] - centre["x"]) * side > 0),
                        key=lambda n: (positions[n]["y"], n))
        heights = [max(27., 13. * len(groups[n]) + 10.) for n in leaves]
        y = centre["y"] - sum(heights) / 2
        for leaf, height in zip(leaves, heights):
            pos = positions[leaf]
            x = centre["x"] + side * max(abs(pos["x"] - centre["x"]), 132. + pos.get("width", 0) / 2)
            new_y = y + height / 2
            dx, dy = x - pos["x"], new_y - pos["y"]
            pos.update(x=x, y=new_y)
            for key in ("start_xy", "end_xy"):
                if point(pos.get(key)):
                    pos[key] = [pos[key][0] + dx, pos[key][1] + dy]
            y += height
            group = sorted(groups[leaf], key=lambda e: (e.get("~type", ""), e["~id"]))
            for i, edge in enumerate(group):
                offset = i - (len(group) - 1) / 2
                start = [centre["x"] + side * centre.get("width", 0) / 2, centre["y"]]
                end = [x - side * pos.get("width", 0) / 2, new_y]
                cp = towards(start, end, .60)
                step = min(23., .5 * math.dist(start, end) / max(1, len(group) - 1))
                cp[1] += offset * step
                if edge["~start"] != hub:
                    start, end = end, start
                route = routes[edge["~id"]]
                route.update(source_port=start, target_port=end, control_points=[cp],
                             route_type="unbundled-bezier")
                route.pop("waypoints", None)
                route.pop("label_anchor", None)
                route.pop("tangent_mode", None)


def native_body(raw):
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return raw
    routes = data.get("edge_routes") if isinstance(data, dict) else None
    if not isinstance(routes, dict):
        return raw
    changed = False
    for route in routes.values():
        cp = controls(route) if isinstance(route, dict) else None
        if cp:
            route.update(route_type="unbundled-bezier", control_points=cp)
            route.pop("waypoints", None)
            route.pop("tangent_mode", None)
            changed = True
    if not changed:
        return raw
    spread_hub_routes(data, routes)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return body if len(body) <= RESPONSE_BYTE_LIMIT else raw


class NativeCurveResults:
    """Apply the same read-time projection to local and proxied dev results."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] != "GET"
                or not scope["path"].startswith("/api/results/")):
            return await self.app(scope, receive, send)
        start = None
        body = bytearray()

        async def project(message):
            nonlocal start
            if message["type"] == "http.response.start" and message["status"] == 200:
                headers = dict(message.get("headers", []))
                if (headers.get(b"content-type", b"").startswith(b"application/json")
                        and b"content-encoding" not in headers):
                    start = message
                    return
            if message["type"] == "http.response.body" and start is not None:
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > RESPONSE_BYTE_LIMIT:
                    await send(start)
                    start = None
                    if body:
                        await send({"type": "http.response.body", "body": bytes(body), "more_body": True})
                    return await send(message)
                body.extend(chunk)
                if not message.get("more_body"):
                    raw = native_body(bytes(body))
                    headers = [(k,v) for k,v in start["headers"] if k not in (b"content-length", b"etag")]
                    await send({**start, "headers": headers + [(b"content-length", str(len(raw)).encode())]})
                    await send({"type": "http.response.body", "body": raw})
                return
            await send(message)

        await self.app(scope, receive, project)
