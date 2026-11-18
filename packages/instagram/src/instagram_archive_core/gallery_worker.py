"""Gallery-dl entrypoint with a fail-closed Local media catalog with SQLite."""
import sys
import os
import inspect
import shutil
from pathlib import Path

from .catalog import LocalArchive, known_media, known_post_id, save_post, remember_target
from instagram_archive_core.archive_identity import bind_target
from instagram_archive_core.gallery_diagnostics import trace_request, trace_job
from gallery_dl import archive, job, main, exception
from gallery_dl.extractor.instagram import InstagramExtractor, InstagramAvatarExtractor
from instagram_archive_core.instagram_avatar import avatar_posts
from instagram_archive_core.instagram_graphql import install_profile_query_compat
from instagram_archive_core.instagram_web_gallery import install_web_graphql

archives = []
archive_signature = inspect.signature(archive.connect)


def require_video_tools():
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name)]
    if missing:
        raise RuntimeError('缺少影片處理工具 ' + ', '.join(missing) +
                           '；請先安裝 FFmpeg，才能合併 Instagram 的影像與音軌。尚未開始抓取。')


def parse_with_boundary(original):
    def parse(self, raw):
        post = bind_target(self, original(self, raw))
        ids = [str(file['media_id']) for file in post.get('_files', [])]
        # The parser can omit an unsupported child. Such a partial carousel
        # must never be treated as a fully known post at the boundary.
        children = raw.get('carousel_media') or [edge.get('node',{}) for edge in raw.get('edge_sidecar_to_children',{}).get('edges',[])]
        expected = [str(child.get('pk') or child.get('id') or '').split('_')[0] for child in children] or ids
        post['_archive_post_ids'] = expected
        complete = bool(expected) and all(i.isdigit() and known_media(i) for i in expected)
        target = post.get('archive_target')
        # A shared post already downloaded for another profile is not this
        # profile's incremental boundary. Record its membership before skipping.
        previous = known_post_id(target, post['post_id'], before=os.environ.get('ARCHIVE_BOUNDARY_BEFORE')) if target and complete and self.subcategory in ('posts','reels') else False
        if target and complete:
            remember_target(expected, post)
            if self.subcategory in ('posts','reels'):
                save_post(target, {'id':str(post['post_id']), 'child_ids':expected})
        if (self.subcategory in ('posts','reels') and not self.config('archive-full-check', False)
                and not post.get('pinned') and complete and (previous if target else True)):
            self.log.info('已遇到完整保存的貼文，停止往更舊內容抓取')
            raise exception.StopExtraction()
        return post
    return parse


parser_names = [name for name in ('_parse_post','_parse_post_rest','_parse_post_graphql') if hasattr(InstagramExtractor,name)]
if not parser_names:
    raise RuntimeError('Unsupported gallery-dl Instagram parser; refusing untracked download')


def connect(*args, **kwargs):
    options = archive_signature.bind(*args, **kwargs)
    options.apply_defaults()
    if options.arguments['prefix'] != 'instagram' or options.arguments['format'] != '{media_id}':
        raise ValueError('Unsupported archive identity')
    value = LocalArchive(options.arguments.get('pathfmt'))
    archives.append(value)
    return value


original_initialize = job.DownloadJob.initialize


def initialize(self, *args, **kwargs):
    original_initialize(self, *args, **kwargs)
    if self.archive is None:
        raise RuntimeError('Local media catalog unavailable; refusing untracked downloads')


def install():
    install_profile_query_compat()
    install_web_graphql()
    InstagramExtractor.request = trace_request(InstagramExtractor.request)
    job.Job.run = trace_job(job.Job.run)
    InstagramAvatarExtractor.posts = avatar_posts
    for name in parser_names:
        setattr(InstagramExtractor,name,parse_with_boundary(getattr(InstagramExtractor,name)))
    archive.connect = connect
    job.DownloadJob.initialize = initialize


if __name__ == '__main__':
    from .storage import guard_disk
    guard_disk()
    install()
    try:
        try:
            require_video_tools()
        except RuntimeError as error:
            print('[instagram][error] ' + str(error), file=sys.stderr, flush=True)
            raise SystemExit(1)
        main()
    finally:
        for value in archives:
            value.close()
