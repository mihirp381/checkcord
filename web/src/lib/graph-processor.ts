import { DirectedGraph } from "graphology";

export interface GraphNode {
  id: string;
  type: "file" | "symbol" | "context" | "developer";
  label: string;
  node_type?: string;
  confidence_score?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
}

export interface NetworkGraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/**
 * Modern styling palette for the network graph visualization.
 * Tailored HSL colors mapped to hex values for canvas rendering.
 */
export const THEME_PALETTE = {
  file: {
    color: "#94a3b8", // Slate 400
    size: 6,
  },
  symbol: {
    color: "#818cf8", // Indigo 400
    size: 8,
  },
  context: {
    color: "#fbbf24", // Amber 400
    size: 10,
  },
  developer: {
    color: "#34d399", // Emerald 400
    size: 5,
  },
};

/**
 * Transforms the API network graph payload into a Graphology DirectedGraph instance.
 * Applies color coding, size styling, and initial random layout coordinates.
 */
export function processNetworkGraph(data: NetworkGraphResponse): DirectedGraph {
  const graph = new DirectedGraph();

  if (!data || !data.nodes) {
    return graph;
  }

  // 1. Add all nodes with visual attributes
  data.nodes.forEach((node) => {
    // Avoid double addition if duplicates occur
    if (graph.hasNode(node.id)) {
      return;
    }

    const style = THEME_PALETTE[node.type] || { color: "#cccccc", size: 4 };

    // Format display label to be cleaner
    let cleanLabel = node.label;
    if (node.type === "context" && cleanLabel.length > 50) {
      cleanLabel = cleanLabel.substring(0, 47) + "...";
    }

    graph.addNode(node.id, {
      label: cleanLabel,
      fullLabel: node.label,
      // Sigma reads `type` as the name of a render program, so the node kind lives in `kind`.
      kind: node.type,
      color: style.color,
      size: style.size,
      // Random coordinates in a small unit square to allow ForceAtlas2 to start detangling
      x: Math.random() * 10 - 5,
      y: Math.random() * 10 - 5,
      confidence_score: node.confidence_score,
      node_type: node.node_type,
    });
  });

  // 2. Add all edges
  if (data.edges) {
    data.edges.forEach((edge) => {
      // Safeguard: make sure both nodes exist in the graph before linking
      if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
        if (!graph.hasEdge(edge.source, edge.target)) {
          graph.addEdge(edge.source, edge.target, {
            color: "#475569", // Slate 600 edge color
            size: 1,
            type: "arrow", // Directed arrow
          });
        }
      }
    });
  }

  return graph;
}
