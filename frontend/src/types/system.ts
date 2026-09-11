export type ContractInfo = {
  format: string;
  version: string;
  fingerprint_algorithm: string;
};

export type HealthStatus = {
  status: string;
  version: string;
  workspace_root: string;
  max_upload_mb: number;
  modelarena_configured: boolean;
  contract: ContractInfo;
};
