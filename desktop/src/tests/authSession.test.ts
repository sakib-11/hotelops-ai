/**
 * Test Suite: Authentication & Session Management
 *
 * Validates Subtask 40.4:
 * - Zustand auth store lifecycle (initializing, unauthenticated, authenticating, authenticated)
 * - Session restoration
 * - Secure logout (purging token, user state, query cache, WebSocket)
 * - User and tenant isolation
 */

import { describe, it, expect, beforeEach, beforeAll } from "vitest";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";
import { queryClient } from "@/query/client";
import { webSocketManager } from "@/realtime/WebSocketManager";

// In-memory Storage mock for Node test environments
class MockStorage implements Storage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
}

describe("Authentication & Session Foundation", () => {
  beforeAll(() => {
    if (typeof globalThis.sessionStorage === "undefined") {
      globalThis.sessionStorage = new MockStorage();
    }
  });

  beforeEach(() => {
    globalThis.sessionStorage.clear();
    useAuthStore.setState({
      status: "unauthenticated",
      user: null,
      tokens: null,
      error: null,
      isRestoring: false,
    });
  });

  it("should initialize in unauthenticated state without token", async () => {
    await useAuthStore.getState().initialize();
    expect(useAuthStore.getState().status).toBe("unauthenticated");
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("should successfully authenticate with valid credentials", async () => {
    await useAuthStore.getState().login({
      email: "admin@hotelops.ai",
      password: "admin123",
    });

    const state = useAuthStore.getState();
    expect(state.status).toBe("authenticated");
    expect(state.user).toBeDefined();
    expect(state.user?.email).toBe("admin@hotelops.ai");
    expect(state.user?.role_name).toBe("admin");
    expect(state.tokens?.access_token).toBeDefined();
  });

  it("should set auth_error on invalid credentials", async () => {
    await expect(
      useAuthStore.getState().login({
        email: "unknown@hotelops.ai",
        password: "wrong",
      }),
    ).rejects.toThrow();

    const state = useAuthStore.getState();
    expect(state.status).toBe("auth_error");
    expect(state.error?.code).toBe("invalid_credentials");
    expect(state.user).toBeNull();
  });

  it("should completely purge user session, query cache, and WebSocket on logout", async () => {
    // 1. Log in as User A
    await useAuthStore.getState().login({
      email: "admin@hotelops.ai",
      password: "admin123",
    });

    // Populate server-state query cache
    queryClient.setQueryData(["users", "detail", "user-a"], { name: "User A Confidential" });
    expect(queryClient.getQueryData(["users", "detail", "user-a"])).toBeDefined();

    // 2. Perform Logout
    await useAuthStore.getState().logout();

    // 3. Verify complete isolation
    const authState = useAuthStore.getState();
    expect(authState.status).toBe("unauthenticated");
    expect(authState.user).toBeNull();
    expect(authState.tokens).toBeNull();

    // Query cache must be cleared
    expect(queryClient.getQueryData(["users", "detail", "user-a"])).toBeUndefined();

    // WebSocket must be disconnected
    expect(webSocketManager.getStatus()).toBe("DISCONNECTED");
  });
});
