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
      <h2 className="text-lg font-semibold mb-3">Versions</h2>
      <div className="bg-gray-50 rounded-md overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-100">
            <tr>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Version</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Hash</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Created</th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {versions.map((version) => (
              <tr key={`${version.agent_id}-${version.version}`}>
                <td className="px-4 py-2 whitespace-nowrap text-sm font-medium text-gray-900">
                  {version.version}
                </td>
                <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 font-mono">
                  {version.spec_hash.slice(0, 8)}
                </td>
                <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500">
                  {new Date(version.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="px-4 py-2 bg-gray-50 text-sm text-gray-500">
          Showing {versions.length} version{versions.length !== 1 ? "s" : ""}
        </div>
      </div>
    </div>
  );
}
