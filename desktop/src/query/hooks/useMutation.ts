/**
 * Mutation Hooks - Server-state mutations for API operations.
 *
 * Provides consistent mutation patterns with proper cache invalidation.
 * Uses the normalized ApiError from Task 40.5.
 */

import {
  useMutation,
  type UseMutationOptions,
  type UseMutationResult,
} from "@tanstack/react-query";
import { queryClient } from "../client";
import { type ApiError } from "@/api/types";

export interface RollbackContext<TContext = unknown> {
  previousData: { key: readonly unknown[]; data: unknown }[];
  customContext?: TContext;
}

/**
 * Generic mutation options with standardized error handling.
 */
export interface BaseMutationOptions<TData, TVariables, TContext = unknown> extends Omit<
  UseMutationOptions<TData, ApiError, TVariables, RollbackContext<TContext>>,
  "mutationFn" | "onMutate" | "onError" | "onSettled" | "onSuccess"
> {
  /**
   * Static query keys to invalidate on successful mutation.
   */
  invalidateKeys?: (() => readonly unknown[])[];

  /**
   * Dynamic query keys to invalidate using the mutation variables / data.
   */
  invalidateDynamicKeys?: (data: TData, variables: TVariables) => (readonly unknown[])[];

  /**
   * Custom onMutate for optimistic updates.
   * Return context for rollback on error.
   */
  onMutate?: (variables: TVariables) => Promise<TContext | undefined> | TContext | undefined;

  /**
   * Called on mutation error.
   */
  onError?: (
    error: ApiError,
    variables: TVariables,
    context: RollbackContext<TContext> | undefined,
  ) => void;

  /**
   * Called on mutation success.
   */
  onSuccess?: (
    data: TData,
    variables: TVariables,
    context: RollbackContext<TContext> | undefined,
  ) => void;

  /**
   * Called after both success and error.
   */
  onSettled?: (
    data: TData | undefined,
    error: ApiError | null,
    variables: TVariables,
    context: RollbackContext<TContext> | undefined,
  ) => void;
}

/**
 * Create a standardized mutation with automatic cache invalidation and rollback.
 */
export function createMutation<TData, TVariables, TContext = unknown>(
  mutationFn: (variables: TVariables) => Promise<TData>,
  options: BaseMutationOptions<TData, TVariables, TContext> = {},
): UseMutationResult<TData, ApiError, TVariables, RollbackContext<TContext>> {
  const {
    invalidateKeys = [],
    invalidateDynamicKeys,
    onMutate,
    onError,
    onSuccess,
    onSettled,
    ...restOptions
  } = options;

  return useMutation<TData, ApiError, TVariables, RollbackContext<TContext>>({
    mutationFn,
    onMutate: async (variables) => {
      // Cancel outgoing refetches for affected static keys
      await Promise.all(
        invalidateKeys.map((key) => queryClient.cancelQueries({ queryKey: key() })),
      );

      // Snapshot previous values for rollback
      const previousData = invalidateKeys.map((key) => ({
        key: key(),
        data: queryClient.getQueryData(key()),
      }));

      // Call custom onMutate if provided
      const customContext = onMutate ? await onMutate(variables) : undefined;

      return {
        previousData,
        customContext,
      };
    },
    onError: (error, variables, context) => {
      // Rollback to previous data
      if (context?.previousData) {
        context.previousData.forEach(({ key, data }) => {
          queryClient.setQueryData(key, data);
        });
      }

      // Call custom onError if provided
      onError?.(error, variables, context);
    },
    onSuccess: (data, variables, context) => {
      // Invalidate static affected queries
      invalidateKeys.forEach((key) => {
        queryClient.invalidateQueries({ queryKey: key() });
      });

      // Invalidate dynamic affected queries
      if (invalidateDynamicKeys) {
        const dynamicKeys = invalidateDynamicKeys(data, variables);
        dynamicKeys.forEach((key) => {
          queryClient.invalidateQueries({ queryKey: key });
        });
      }

      // Call custom onSuccess if provided
      onSuccess?.(data, variables, context);
    },
    onSettled: (data, error, variables, context) => {
      onSettled?.(data, error, variables, context);
    },
    ...restOptions,
  });
}

/**
 * Standardized mutation for CREATE operations.
 * Invalidates list queries on success.
 */
export function useCreateMutation<TData, TVariables>(
  mutationFn: (variables: TVariables) => Promise<TData>,
  listQueryKey: () => readonly unknown[],
  options?: Omit<BaseMutationOptions<TData, TVariables>, "invalidateKeys">,
) {
  return createMutation<TData, TVariables>(mutationFn, {
    invalidateKeys: [listQueryKey],
    ...options,
  });
}

/**
 * Standardized mutation for UPDATE operations.
 * Invalidates both list and detail queries on success.
 */
export function useUpdateMutation<TData, TVariables extends { id: string }>(
  mutationFn: (variables: TVariables) => Promise<TData>,
  listQueryKey: () => readonly unknown[],
  detailQueryKey: (id: string) => readonly unknown[],
  options?: Omit<
    BaseMutationOptions<TData, TVariables, { previousDetail: unknown }>,
    "invalidateKeys"
  >,
) {
  return createMutation<TData, TVariables, { previousDetail: unknown }>(mutationFn, {
    invalidateKeys: [listQueryKey],
    invalidateDynamicKeys: (_data, variables) => [detailQueryKey(variables.id)],
    onMutate: async (variables) => {
      const key = detailQueryKey(variables.id);
      await queryClient.cancelQueries({ queryKey: key });
      const previousDetail = queryClient.getQueryData(key);
      return { previousDetail };
    },
    onError: (error, variables, context) => {
      if (context?.customContext?.previousDetail !== undefined) {
        queryClient.setQueryData(
          detailQueryKey(variables.id),
          context.customContext.previousDetail,
        );
      }
      options?.onError?.(error, variables, context);
    },
    ...options,
  });
}

/**
 * Standardized mutation for DELETE operations.
 * Removes from list and detail cache.
 */
export function useDeleteMutation<TVariables extends { id: string }>(
  mutationFn: (variables: TVariables) => Promise<void>,
  listQueryKey: () => readonly unknown[],
  detailQueryKey: (id: string) => readonly unknown[],
  options?: Omit<
    BaseMutationOptions<void, TVariables, { previousList: unknown; previousDetail: unknown }>,
    "invalidateKeys"
  >,
) {
  return createMutation<void, TVariables, { previousList: unknown; previousDetail: unknown }>(
    mutationFn,
    {
      invalidateKeys: [listQueryKey],
      onMutate: async (variables) => {
        const listKey = listQueryKey();
        const detailKey = detailQueryKey(variables.id);

        await Promise.all([
          queryClient.cancelQueries({ queryKey: listKey }),
          queryClient.cancelQueries({ queryKey: detailKey }),
        ]);

        const previousList = queryClient.getQueryData(listKey);
        const previousDetail = queryClient.getQueryData(detailKey);

        // Optimistically remove from list
        queryClient.setQueryData(listKey, (old: unknown) => {
          if (!old || !Array.isArray(old)) return old;
          return old.filter((item: { id?: string }) => item?.id !== variables.id);
        });

        queryClient.removeQueries({ queryKey: detailKey });

        return { previousList, previousDetail };
      },
      onError: (error, variables, context) => {
        if (context?.customContext?.previousList !== undefined) {
          queryClient.setQueryData(listQueryKey(), context.customContext.previousList);
        }
        if (context?.customContext?.previousDetail !== undefined) {
          queryClient.setQueryData(
            detailQueryKey(variables.id),
            context.customContext.previousDetail,
          );
        }
        options?.onError?.(error, variables, context);
      },
      ...options,
    },
  );
}

/**
 * Hook for optimistic update of a single item in a list.
 * Updates the item in the list cache immediately, then syncs with server.
 */
export function useOptimisticUpdate<TItem extends { id: string }>(
  listQueryKey: () => readonly unknown[],
  detailQueryKey: (id: string) => readonly unknown[],
  mutationFn: (item: Partial<TItem> & { id: string }) => Promise<TItem>,
) {
  return useMutation({
    mutationFn,
    onMutate: async (updatedItem) => {
      const listKey = listQueryKey();
      const detailKey = detailQueryKey(updatedItem.id);

      await Promise.all([
        queryClient.cancelQueries({ queryKey: listKey }),
        queryClient.cancelQueries({ queryKey: detailKey }),
      ]);

      const previousList = queryClient.getQueryData(listKey);
      const previousDetail = queryClient.getQueryData(detailKey);

      queryClient.setQueryData(listKey, (old: unknown) => {
        if (!old || !Array.isArray(old)) return old;
        return old.map((item: TItem) =>
          item.id === updatedItem.id ? { ...item, ...updatedItem } : item,
        );
      });

      queryClient.setQueryData(detailKey, (old: TItem | undefined) =>
        old ? { ...old, ...updatedItem } : updatedItem,
      );

      return { previousList, previousDetail };
    },
    onError: (_error, variables, context) => {
      if (context?.previousList !== undefined) {
        queryClient.setQueryData(listQueryKey(), context.previousList);
      }
      if (context?.previousDetail !== undefined) {
        queryClient.setQueryData(detailQueryKey(variables.id), context.previousDetail);
      }
    },
    onSuccess: (data, variables) => {
      queryClient.setQueryData(detailQueryKey(variables.id), data);
      queryClient.setQueryData(listQueryKey(), (old: unknown) => {
        if (!old || !Array.isArray(old)) return old;
        return old.map((item: TItem) => (item.id === variables.id ? data : item));
      });
    },
    onSettled: (_data, _error, variables) => {
      queryClient.invalidateQueries({ queryKey: detailQueryKey(variables.id) });
      queryClient.invalidateQueries({ queryKey: listQueryKey() });
    },
  });
}
