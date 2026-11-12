import React from "react";

interface AgentVersion {
  agent_id: string;
  version: number;
  spec_hash: string;
  created_at: string;
}

interface AgentVersionsPanelProps {
  versions: AgentVersion[];
}

export function AgentVersionsPanel({ versions }: AgentVersionsPanelProps) {
  if (versions.length === 0) {
    return null;
  }

  return (
    <div className="border-t pt-6">
      <h2 className="text-lg font-semibold mb-3 text-card-foreground">Versions</h2>
      <div className="bg-muted rounded-md overflow-hidden">
        <table className="min-w-full divide-y divide-border">
          <thead className="bg-muted">
            <tr>
              <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Version</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Hash</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Created</th>
            </tr>
          </thead>
          <tbody className="bg-card divide-y divide-border">
            {versions.map((version) => (
              <tr key={`${version.agent_id}-${version.version}`}>
                <td className="px-4 py-2 whitespace-nowrap text-sm font-medium text-card-foreground">
                  {version.version}
                </td>
                <td className="px-4 py-2 whitespace-nowrap text-sm text-muted-foreground font-mono">
                  {version.spec_hash.slice(0, 8)}
                </td>
                <td className="px-4 py-2 whitespace-nowrap text-sm text-muted-foreground">
                  {new Date(version.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="px-4 py-2 bg-muted text-sm text-muted-foreground">
          Showing {versions.length} version{versions.length !== 1 ? "s" : ""}
        </div>
      </div>
    </div>
  );
}
