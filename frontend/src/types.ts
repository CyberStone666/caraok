export type Category = {
  id: string;
  name: string;
  description: string;
  count: number;
};

export type Tier = 'primary' | 'secondary' | null;

export type AccessToken = {
  id: string;
  name: string;
  created_at: number;
  last_used_at: number | null;
};

export type Track = {
  id: string;
  title: string;
  artist: string;
  category_id: string;
  category_name: string;
  description: string;
  cover_url: string | null;
  mime_type: string;
};

export type TrackFileStatus = {
  source: 'local' | 'remote';
  exists?: boolean;
  size_bytes?: number;
  file_name?: string;
  url_host?: string;
  detail?: string;
};

export type AdminTrack = {
  id: string;
  title: string;
  artist: string;
  category_id: string;
  category_name: string;
  description: string;
  mime_type: string;
  enabled: boolean;
  order: number;
  url_host: string;
  file: TrackFileStatus;
};

export type DownloadJob = {
  job_id: string;
  state: 'queued' | 'downloading' | 'remuxing' | 'done' | 'error';
  progress: number;
  slug: string;
  title: string;
  category_id: string;
  track_id: string;
  error: string;
  youtube_video_id: string;
  created_at: number;
};
