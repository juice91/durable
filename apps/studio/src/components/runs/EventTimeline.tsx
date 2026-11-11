import React from "react";
import { type RunEvent, type RunDelta, type PartialTextMap } from "@/lib/runs";

interface EventItemProps {
  event: RunEvent;
}

interface EventTimelineProps {
  events: RunEvent[];
  currentPartialText: PartialTextMap;
}

export function EventTimeline({ events, currentPartialText }: EventTimelineProps) {
  if (events.length === 0) {
    return (
      <div className="p-4 text-gray-500 bg-gray-50 rounded-lg text-center">
        No events yet. Run started, check back soon.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {events.map((event) => (
        <EventItem key={event.seq} event={event} />
      ))}

      {/* Show current partial text under active model request */}
      {Object.entries(currentPartialText).length > 0 && (
        <div className="mt-4 p-4 bg-blue-50 border border-blue-200 rounded-lg">
          <h4 className="text-sm font-semibold text-blue-800 mb-2">Live partial text</h4>
          <div className="space-y-2">
            {Object.entries(currentPartialText).map(([modelRequest, partial]) => (
              <div key={modelRequest} className="text-sm text-blue-700">
                <span className="font-mono text-xs text-blue-500 mr-2">
                  Request #{modelRequest}
                </span>
                <span>{partial.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function EventItem({ event }: EventItemProps) {
  const renderContent = () => {
    switch (event.type) {
      case "run_started":
        return (
          <div className="text-sm text-gray-600">
            Run started
            {event.data.input && (
              <span className="block mt-1 text-xs font-mono text-gray-500">
                Input: {event.data.input}
              </span>
            )}
          </div>
        );
      case "model_message":
        return (
          <div className="text-sm text-gray-800">
            <span className="font-semibold">Model response:</span>
            <p className="mt-1 text-gray-700">{event.data.text}</p>
          </div>
        );
      case "tool_call":
        return (
          <div className="text-sm">
            <span className="font-semibold text-purple-700">Tool call:</span>
            <span className="text-purple-600 ml-1">
              {event.data.tool_name}
            </span>
            <pre className="mt-1 text-xs text-gray-500 bg-gray-100 p-2 rounded">
              {JSON.stringify(event.data.args, null, 2)}
            </pre>
          </div>
        );
      case "tool_result":
        return (
          <div className="text-sm">
            <span className="font-semibold text-green-700">Tool result:</span>
            <pre className="mt-1 text-xs text-gray-500 bg-gray-100 p-2 rounded">
              {JSON.stringify(event.data.content, null, 2)}
            </pre>
          </div>
        );
      case "run_completed":
        return (
          <div className="text-sm text-green-700 font-semibold">
            Run completed successfully
          </div>
        );
      case "run_failed":
        return (
          <div className="text-sm text-red-700 font-semibold">
            Run failed
            <span className="block mt-1 text-red-600">
              {event.data.error || "Unknown error"}
            </span>
          </div>
        );
      default:
        return <div className="text-sm text-gray-500">{JSON.stringify(event.data)}</div>;
    }
  };

  return (
    <div className="flex gap-3">
      <div className="flex-shrink-0 w-12 pt-1">
        <div className="text-xs text-gray-500 font-mono">
          seq {event.seq}
        </div>
        <div className="text-xs text-gray-400 mt-0.5">
          {new Date(event.ts).toLocaleTimeString()}
        </div>
      </div>
      <div className="flex-1">
        <div className="text-xs font-semibold text-gray-700 mb-1 uppercase tracking-wide">
          {event.type}
        </div>
        {renderContent()}
      </div>
    </div>
  );
}
