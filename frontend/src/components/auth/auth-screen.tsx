"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import { authErrorMessage, login, signup } from "@/lib/api/auth";
import { meQueryKey } from "./session-gate";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Mode = "login" | "signup";

export function AuthScreen() {
  const qc = useQueryClient();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") await login(email, password);
      else await signup(email, password, orgName);
      // The cookie is set; re-reading /me flips the gate.
      await qc.invalidateQueries({ queryKey: meQueryKey });
    } catch (err) {
      setError(
        err instanceof ApiError
          ? authErrorMessage(err.message)
          : "Couldn't reach the DataTalk API.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-[380px]">
        <div className="mb-8">
          <h1 className="text-[24px] font-semibold tracking-[-0.015em] text-ink-primary">
            DataTalk
          </h1>
          <p className="mt-1 text-[13px] text-ink-secondary">
            Ask your warehouse a question. Every number cites its query.
          </p>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <Label htmlFor="email" className="label-caps text-ink-secondary">
              Email
            </Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1.5 bg-muted"
            />
          </div>

          <div>
            <Label htmlFor="password" className="label-caps text-ink-secondary">
              Password
            </Label>
            <Input
              id="password"
              type="password"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1.5 bg-muted"
            />
          </div>

          {mode === "signup" && (
            <div>
              <Label htmlFor="org" className="label-caps text-ink-secondary">
                Workspace name
              </Label>
              <Input
                id="org"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                placeholder="Optional"
                className="mt-1.5 bg-muted"
              />
            </div>
          )}

          {error && (
            <p
              role="alert"
              className="border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
            >
              {error}
            </p>
          )}

          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>

        <p className="mt-6 text-[13px] text-ink-secondary">
          {mode === "login" ? "No account yet?" : "Already have an account?"}{" "}
          <button
            type="button"
            onClick={() => {
              setMode(mode === "login" ? "signup" : "login");
              setError(null);
            }}
            className="text-link underline underline-offset-2"
          >
            {mode === "login" ? "Create one" : "Sign in"}
          </button>
        </p>
      </div>
    </div>
  );
}
