import api from "./client";

export type InventoryFormat = "csv" | "json";

export type InventoryQuery = {
  format: InventoryFormat;
  columns: string[];
  filters?: Record<string, string | string[] | undefined>;
};

export async function downloadInventory(query: InventoryQuery): Promise<void> {
  const params = new URLSearchParams();
  params.set("format", query.format);
  if (query.columns.length > 0) {
    params.set("columns", query.columns.join(","));
  }
  for (const [key, value] of Object.entries(query.filters ?? {})) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      value.forEach((v) => params.append(key, v));
    } else {
      params.set(key, value);
    }
  }
  const response = await api.get(`/devices/inventory?${params.toString()}`, {
    responseType: "blob",
  });
  const blob = response.data as Blob;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const ext = query.format === "csv" ? "csv" : "json";
  triggerBrowserDownload(blob, `gridfleet-inventory-${stamp}.${ext}`);
}

function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
