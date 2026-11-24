import { describe, it, expect } from "vitest";
import { processNetworkGraph, NetworkGraphResponse } from "../../src/lib/graph-processor";

describe("graph-processor.ts unit tests", () => {
  it("should process a valid network graph payload correctly", () => {
    const payload: NetworkGraphResponse = {
      nodes: [
        { id: "file:src/calc.py", type: "file", label: "src/calc.py" },
        { id: "symbol:node-1", type: "symbol", label: "calculate_total", node_type: "function" },
        { id: "context:ctx-1", type: "context", label: "Check calculations", confidence_score: 9.2 },
        { id: "developer:Alice", type: "developer", label: "Alice" },
      ],
      edges: [
        { source: "file:src/calc.py", target: "symbol:node-1" },
        { source: "symbol:node-1", target: "context:ctx-1" },
        { source: "context:ctx-1", target: "developer:Alice" },
      ],
    };

    const graph = processNetworkGraph(payload);

    expect(graph.order).toBe(4);
    expect(graph.size).toBe(3);

    // Verify node attributes
    expect(graph.getNodeAttribute("file:src/calc.py", "kind")).toBe("file");
    expect(graph.getNodeAttribute("file:src/calc.py", "color")).toBe("#94a3b8"); // slate
    expect(graph.getNodeAttribute("file:src/calc.py", "size")).toBe(6);

    expect(graph.getNodeAttribute("symbol:node-1", "kind")).toBe("symbol");
    expect(graph.getNodeAttribute("symbol:node-1", "color")).toBe("#818cf8"); // indigo
    expect(graph.getNodeAttribute("symbol:node-1", "node_type")).toBe("function");

    expect(graph.getNodeAttribute("context:ctx-1", "kind")).toBe("context");
    expect(graph.getNodeAttribute("context:ctx-1", "color")).toBe("#fbbf24"); // amber
    expect(graph.getNodeAttribute("context:ctx-1", "confidence_score")).toBe(9.2);

    expect(graph.getNodeAttribute("developer:Alice", "kind")).toBe("developer");
    expect(graph.getNodeAttribute("developer:Alice", "color")).toBe("#34d399"); // emerald

    graph.forEachNode((_, attrs) => expect(attrs.type).toBeUndefined());

    // Verify edges
    expect(graph.hasEdge("file:src/calc.py", "symbol:node-1")).toBe(true);
    expect(graph.hasEdge("symbol:node-1", "context:ctx-1")).toBe(true);
    expect(graph.hasEdge("context:ctx-1", "developer:Alice")).toBe(true);
  });

  it("should handle duplicate nodes and edges gracefully", () => {
    const payload: NetworkGraphResponse = {
      nodes: [
        { id: "file:src/calc.py", type: "file", label: "src/calc.py" },
        { id: "file:src/calc.py", type: "file", label: "src/calc.py" }, // duplicate
      ],
      edges: [
        { source: "file:src/calc.py", target: "file:src/calc.py" },
        { source: "file:src/calc.py", target: "file:src/calc.py" }, // duplicate
      ],
    };

    const graph = processNetworkGraph(payload);
    expect(graph.order).toBe(1);
    expect(graph.size).toBe(1);
  });

  it("should handle empty or malformed inputs", () => {
    const emptyPayload: NetworkGraphResponse = { nodes: [], edges: [] };
    const graph1 = processNetworkGraph(emptyPayload);
    expect(graph1.order).toBe(0);
    expect(graph1.size).toBe(0);

    // @ts-expect-error null is not a valid payload
    const graph2 = processNetworkGraph(null);
    expect(graph2.order).toBe(0);
    expect(graph2.size).toBe(0);
  });

  it("should truncate long context summaries for label styling", () => {
    const longSummary = "A".repeat(100);
    const payload: NetworkGraphResponse = {
      nodes: [{ id: "context:ctx-1", type: "context", label: longSummary }],
      edges: [],
    };

    const graph = processNetworkGraph(payload);
    const label = graph.getNodeAttribute("context:ctx-1", "label");
    expect(label.length).toBe(50);
    expect(label.endsWith("...")).toBe(true);
    expect(graph.getNodeAttribute("context:ctx-1", "fullLabel")).toBe(longSummary);
  });
});
