CREATE UNIQUE INDEX generated_srt_feed_port_unique
    ON feeds (tenant_id, ((config ->> 'feed_port')::integer))
    WHERE config ->> 'source' = 'generated'
      AND config ->> 'protocol' = 'srt';
