import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { post, get, templateData } = vi.hoisted(() => ({
  post: vi.fn(),
  get: vi.fn(),
  templateData: [] as Array<Record<string, unknown>>,
}));

vi.mock("../src/api/client", () => ({
  api: { get, post },
  message: (error: unknown) => String(error),
}));

vi.mock("../src/hooks/data", () => ({
  useAmis: () => ({ data: [], isLoading: false }),
  useKeyPairs: () => ({ data: [] }),
  useSecurityGroups: () => ({ data: [], isLoading: false }),
  useTemplates: () => ({ data: templateData }),
  useInstances: () => ({ data: [] }),
  useOperations: () => ({ data: [] }),
  useInstanceAction: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { LaunchPage } from "../src/pages/Instances";

describe("instance launch form", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.clearAllMocks();
    templateData.splice(0);
    get.mockImplementation((url: string) => {
      if (url === "/instances/capacity/zones") {
        return Promise.resolve({
          data: [{ availability_zone: "local-region-1c", available: true }],
        });
      }
      return Promise.resolve({ data: [] });
    });
    post.mockResolvedValue({ data: { instance_id: "new-instance" } });
  });

  it("submits a manual launch to the API", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/instances/new"]}>
          <Routes>
            <Route path="/instances/new" element={<LaunchPage />} />
            <Route path="/instances/:instanceId" element={<p>Instance accepted</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "web-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Launch instance" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(
      "/instances",
      expect.objectContaining({
        name: "web-01",
        image: "24.04",
        launch_template_id: null,
      }),
    );
    expect(await screen.findByText("Instance accepted")).toBeInTheDocument();
  });

  it("submits locked template values to the API", async () => {
    templateData.push({
      id: "template-1",
      name: "Web template",
      description: "Template configuration",
      image: "22.04",
      instance_type: "t4.small",
      cpu_count: 2,
      memory_mb: 2048,
      disk_gb: 20,
      availability_zones: ["local-region-1c"],
      security_group_ids: [],
      user_data: "#!/bin/sh\necho ready",
      key_pair_id: null,
      create_key_pair: true,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/instances/new"]}>
          <Routes>
            <Route path="/instances/new" element={<LaunchPage />} />
            <Route path="/instances/:instanceId" element={<p>Template accepted</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getAllByRole("combobox")[0]!, {
      target: { value: "template-1" },
    });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "template-web" } });
    fireEvent.click(screen.getByRole("button", { name: "Launch instance" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(
      "/instances",
      expect.objectContaining({
        name: "template-web",
        launch_template_id: "template-1",
        image: "22.04",
        instance_type: "t4.small",
        cpu_count: 2,
        memory_mb: 2048,
        disk_gb: 20,
        availability_zone: "local-region-1c",
      }),
    );
    expect(await screen.findByText("Template accepted")).toBeInTheDocument();
  });
});
