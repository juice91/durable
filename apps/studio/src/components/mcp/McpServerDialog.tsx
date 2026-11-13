"use client";

import React from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import type { McpServer } from "@/lib/mcpServers";
import { McpServerForm } from "./McpServerForm";

interface McpServerDialogProps {
  /** The server being edited; absent to add a new one. */
  server?: McpServer;
  onSaved: (server: McpServer | null) => void;
  onClose: () => void;
}

/** "Add MCP server" or "Edit {name}": McpServerForm in a modal Dialog. Mounted while open. */
export function McpServerDialog({ server, onSaved, onClose }: McpServerDialogProps) {
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="md:max-w-2xl" data-testid="mcp-server-dialog">
        <DialogHeader>
          <DialogTitle>{server ? `Edit ${server.name}` : "Add MCP server"}</DialogTitle>
          <DialogDescription>
            {server ? "Header values are never shown again; replace a header to change it." : "A remote server we connect to over Streamable HTTP. Its tools become available to agents."}
          </DialogDescription>
        </DialogHeader>
        <McpServerForm key={server?.id ?? "create"} server={server} onSaved={onSaved} onCancel={onClose} embedded />
      </DialogContent>
    </Dialog>
  );
}
