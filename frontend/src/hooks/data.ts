import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Ami,
  Group,
  Instance,
  KeyPair,
  LoadBalancer,
  Operation,
  SecurityGroup,
  Template,
  Volume,
} from "../types";
const get = <T>(url: string) => api.get<T>(url).then((r) => r.data);
export const useInstances = () =>
  useQuery({
    queryKey: ["instances"],
    queryFn: () => get<Instance[]>("/instances"),
    refetchInterval: 5000,
  });
export const useOperations = () =>
  useQuery({
    queryKey: ["operations"],
    queryFn: () => get<Operation[]>("/operations"),
    refetchInterval: (q) =>
      q.state.data?.some((x) => ["PENDING", "RUNNING"].includes(x.status))
        ? 2000
        : 10000,
  });
export const useTemplates = () =>
  useQuery({
    queryKey: ["templates"],
    queryFn: () => get<Template[]>("/launch-templates"),
  });
export const useGroups = () =>
  useQuery({
    queryKey: ["groups"],
    queryFn: () => get<Group[]>("/autoscaling-groups"),
  });
export const useLoadBalancers = () =>
  useQuery({
    queryKey: ["load-balancers"],
    queryFn: () => get<LoadBalancer[]>("/load-balancers"),
    refetchInterval: 5000,
  });
export const useKeyPairs = () =>
  useQuery({
    queryKey: ["key-pairs"],
    queryFn: () => get<KeyPair[]>("/key-pairs"),
  });
export const useSecurityGroups = () =>
  useQuery({
    queryKey: ["security-groups"],
    queryFn: () => get<SecurityGroup[]>("/security-groups"),
  });
export const useVolumes = () =>
  useQuery({
    queryKey: ["volumes"],
    queryFn: () => get<Volume[]>("/volumes"),
    refetchInterval: 5000,
  });
export const useAmis = () =>
  useQuery({
    queryKey: ["amis"],
    queryFn: () => get<Ami[]>("/amis"),
  });
export function useInstanceAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      action,
    }: {
      id: string;
      action: "start" | "stop" | "hibernate" | "retry" | "terminate";
    }) =>
      action === "terminate"
        ? api.delete(`/instances/${id}`)
        : api.post(`/instances/${id}/${action}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["instances"] });
      void qc.invalidateQueries({ queryKey: ["operations"] });
    },
  });
}
