"""
Loads nitw_campus_path_network.json (built from the traced campus roads +
decoded landmark Plus Codes) and provides shortest-path routing over it.
"""
import json
import math
import heapq
from dataclasses import dataclass
from typing import List, Tuple, Dict

EARTH_R_M = 6371000.0


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(h))


def bearing_deg(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dl = lon2 - lon1
    x = math.sin(dl) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def destination_point(start: Tuple[float, float], bearing: float, dist_m: float) -> Tuple[float, float]:
    """Project a point `dist_m` meters from `start` along `bearing` degrees."""
    lat1 = math.radians(start[0])
    lon1 = math.radians(start[1])
    brng = math.radians(bearing)
    delta = dist_m / EARTH_R_M
    lat2 = math.asin(math.sin(lat1) * math.cos(delta) + math.cos(lat1) * math.sin(delta) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(delta) * math.cos(lat1),
        math.cos(delta) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180)


def polyline_length_m(points: List[Tuple[float, float]]) -> float:
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


@dataclass
class Terminal:
    id: int
    name: str
    lat: float
    lon: float
    node: int
    offset_m: float
    plus_code: str = ""


class CampusNetwork:
    """A routable graph of [lat, lon] nodes + undirected weighted edges,
    with named landmark terminals snapped onto the nearest node."""

    def __init__(self, path):
        with open(path) as f:
            raw = json.load(f)

        self.nodes: List[Tuple[float, float]] = [tuple(n) for n in raw["nodes"]]
        self.edges: List[Tuple[int, int, float]] = [tuple(e) for e in raw["edges"]]
        self.terminals: Dict[str, Terminal] = {
            t["name"]: Terminal(**t) for t in raw["terminals"]
        }
        self.terminal_by_id: Dict[int, Terminal] = {t.id: t for t in self.terminals.values()}

        self._adj: Dict[int, List[Tuple[int, float]]] = {i: [] for i in range(len(self.nodes))}
        for a, b, d in self.edges:
            self._adj[a].append((b, d))
            self._adj[b].append((a, d))

    def terminal_list(self) -> List[Terminal]:
        return sorted(self.terminals.values(), key=lambda t: t.name)

    def shortest_path_nodes(self, src_node: int, dst_node: int) -> List[int]:
        """Dijkstra over the node graph. Returns a list of node ids."""
        dist = {src_node: 0.0}
        prev: Dict[int, int] = {}
        visited = set()
        pq = [(0.0, src_node)]
        while pq:
            du, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            if u == dst_node:
                break
            for v, w in self._adj[u]:
                nd = du + w
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst_node not in dist:
            raise ValueError("No path between the requested nodes (graph is disconnected there).")
        path = [dst_node]
        while path[-1] != src_node:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    def shortest_path_latlon(self, src_node: int, dst_node: int) -> List[Tuple[float, float]]:
        return [self.nodes[i] for i in self.shortest_path_nodes(src_node, dst_node)]

    def nearest_node_to(self, lat: float, lon: float) -> int:
        best_i, best_d = 0, float("inf")
        for i, (nlat, nlon) in enumerate(self.nodes):
            d = haversine_m((lat, lon), (nlat, nlon))
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    def as_network_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "terminals": [
                {
                    "id": t.id, "name": t.name, "lat": t.lat, "lon": t.lon,
                    "node": t.node, "offset_m": t.offset_m,
                }
                for t in self.terminal_list()
            ],
        }
