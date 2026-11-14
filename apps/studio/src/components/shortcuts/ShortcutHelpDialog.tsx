"use client";

import React from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { ShortcutTable } from "./ShortcutTable";

export interface ShortcutHelpDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** The `?` dialog: every global shortcut, from the same rows as Settings. */
export function ShortcutHelpDialog({ open, onOpenChange }: ShortcutHelpDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="shortcut-help" className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>Single keys work when focus is not in a text field.</DialogDescription>
        </DialogHeader>
        <ShortcutTable headingLevel={3} />
      </DialogContent>
    </Dialog>
  );
}
