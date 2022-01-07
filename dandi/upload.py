from functools import reduce
import os.path
from pathlib import Path
import re
import time

import click

from . import lgr
from .consts import DRAFT, dandiset_identifier_regex, dandiset_metadata_file
from .exceptions import NotFoundError
from .files import DandisetMetadataFile, LocalAsset, find_dandi_files
from .utils import ensure_datetime, get_instance, pluralize


def upload(
    paths,
    existing="refresh",
    validation="require",
    dandiset_path=None,
    dandi_instance="dandi",
    allow_any_path=False,
    upload_dandiset_metadata=False,
    devel_debug=False,
    jobs=None,
    jobs_per_file=None,
    sync=False,
):
    from .dandiapi import DandiAPIClient
    from .dandiset import APIDandiset, Dandiset

    dandiset = Dandiset.find(dandiset_path)
    if not dandiset:
        raise RuntimeError(
            f"Found no {dandiset_metadata_file} anywhere.  "
            "Use 'dandi download' or 'organize' first"
        )

    instance = get_instance(dandi_instance)
    assert instance.api is not None
    api_url = instance.api

    client = DandiAPIClient(api_url)
    client.check_schema_version()
    client.dandi_authenticate()

    dandiset = APIDandiset(dandiset.path)  # "cast" to a new API based dandiset

    ds_identifier = dandiset.identifier
    remote_dandiset = client.get_dandiset(ds_identifier, DRAFT)

    if not re.match(dandiset_identifier_regex, str(ds_identifier)):
        raise ValueError(
            f"Dandiset identifier {ds_identifier} does not follow expected "
            f"convention {dandiset_identifier_regex!r}."
        )

    from .pynwb_utils import ignore_benign_pynwb_warnings
    from .support.pyout import naturalsize
    from .utils import path_is_subpath

    ignore_benign_pynwb_warnings()  # so validate doesn't whine

    #
    # Treat paths
    #
    if not paths:
        paths = [dandiset.path]
    original_paths = paths

    # Expand and validate all paths -- they should reside within dandiset
    paths = [Path(p).absolute() for p in paths]
    dandi_files = list(
        find_dandi_files(
            *paths,
            dandiset_path=dandiset.path,
            allow_all=allow_any_path,
            include_metadata=True,
        )
    )
    lgr.info(f"Found {len(dandi_files)} files to consider")

    # We will keep a shared set of "being processed" paths so
    # we could limit the number of them until
    #   https://github.com/pyout/pyout/issues/87
    # properly addressed
    process_paths = set()
    from collections import defaultdict

    uploaded_paths = defaultdict(lambda: {"size": 0, "errors": []})

    def skip_file(msg):
        return {"status": "skipped", "message": str(msg)}

    # TODO: we might want to always yield a full record so no field is not
    # provided to pyout to cause it to halt
    def process_path(dfile):
        """

        Parameters
        ----------
        dfile: DandiFile

        Yields
        ------
        dict
          Records for pyout
        """
        strpath = str(dfile.filepath)
        try:
            try:
                yield {"size": dfile.get_size()}
            except FileNotFoundError:
                yield skip_file("ERROR: File not found")
                return
            except Exception as exc:
                # without limiting [:50] it might cause some pyout indigestion
                yield skip_file("ERROR: %s" % str(exc)[:50])
                return

            #
            # Validate first, so we do not bother server at all if not kosher
            #
            # TODO: enable back validation of dandiset.yaml
            if isinstance(dfile, LocalAsset) and validation != "skip":
                yield {"status": "pre-validating"}
                validation_errors = dfile.get_validation_errors()
                yield {"errors": len(validation_errors)}
                # TODO: split for dandi, pynwb errors
                if validation_errors:
                    if validation == "require":
                        yield skip_file("failed validation")
                        return
                else:
                    yield {"status": "validated"}
            else:
                # yielding empty causes pyout to get stuck or crash
                # https://github.com/pyout/pyout/issues/91
                # yield {"errors": '',}
                pass

            #
            # Special handling for dandiset.yaml
            # Yarik hates it but that is life for now. TODO
            #
            if isinstance(dfile, DandisetMetadataFile):
                # TODO This is a temporary measure to avoid breaking web UI
                # dandiset metadata schema assumptions.  All edits should happen
                # online.
                if upload_dandiset_metadata:
                    yield {"status": "updating metadata"}
                    remote_dandiset.set_raw_metadata(dandiset.metadata)
                    yield {"status": "updated metadata"}
                else:
                    yield skip_file("should be edited online")
                return

            #
            # Compute checksums
            #
            yield {"status": "digesting"}
            try:
                file_etag = dfile.get_etag()
            except Exception as exc:
                yield skip_file("failed to compute digest: %s" % str(exc))
                return

            try:
                extant = remote_dandiset.get_asset_by_path(dfile.path)
            except NotFoundError:
                extant = None
            else:
                metadata = extant.get_raw_metadata()
                local_mtime = dfile.get_mtime()
                remote_mtime_str = metadata.get("blobDateModified")
                # TODO: Should this error if the digest is missing?
                extant_etag = metadata.get("digest", {}).get(file_etag.algorithm.value)
                if remote_mtime_str is not None:
                    remote_mtime = ensure_datetime(remote_mtime_str)
                    remote_file_status = (
                        "same"
                        if extant_etag == file_etag.value
                        and remote_mtime == local_mtime
                        else (
                            "newer"
                            if remote_mtime > local_mtime
                            else ("older" if remote_mtime < local_mtime else "diff")
                        )
                    )
                else:
                    remote_mtime = None
                    remote_file_status = "no mtime"

                exists_msg = f"exists ({remote_file_status})"

                if existing == "error":
                    # as promised -- not gentle at all!
                    raise FileExistsError(exists_msg)
                if existing == "skip":
                    yield skip_file(exists_msg)
                    return
                # Logic below only for overwrite and reupload
                if existing == "overwrite":
                    if extant_etag == file_etag.value:
                        yield skip_file(exists_msg)
                        return
                elif existing == "refresh":
                    if extant_etag == file_etag.value:
                        yield skip_file("file exists")
                        return
                    elif remote_mtime is not None and remote_mtime >= local_mtime:
                        yield skip_file(exists_msg)
                        return
                elif existing == "force":
                    pass
                else:
                    raise ValueError(f"invalid value for 'existing': {existing!r}")

                yield {"message": f"{exists_msg} - reuploading"}

            #
            # Extract metadata - delayed since takes time, but is done before
            # actual upload, so we could skip if this fails
            #
            # Extract metadata before actual upload and skip if fails
            # TODO: allow for for non-nwb files to skip this step
            # ad-hoc for dandiset.yaml for now
            yield {"status": "extracting metadata"}
            try:
                metadata = dfile.get_metadata(
                    digest=file_etag, ignore_errors=allow_any_path
                ).json_dict()
            except Exception as e:
                yield skip_file("failed to extract metadata: %s" % str(e))
                return

            #
            # Upload file
            #
            yield {"status": "uploading"}
            validating = False
            for r in remote_dandiset.iter_upload_raw_asset(
                dfile.filepath, metadata, jobs=jobs_per_file, replace_asset=extant
            ):
                r.pop("asset", None)  # to keep pyout from choking
                if r["status"] == "uploading":
                    uploaded_paths[strpath]["size"] = r.pop("current")
                    yield r
                elif r["status"] == "post-validating":
                    # Only yield the first "post-validating" status
                    if not validating:
                        yield r
                        validating = True
                else:
                    yield r
            yield {"status": "done"}

        except Exception as exc:
            if devel_debug:
                raise
            lgr.exception("Error uploading %s:", strpath)
            # Custom formatting for some exceptions we know to extract
            # user-meaningful message
            message = str(exc)
            uploaded_paths[strpath]["errors"].append(message)
            yield {"status": "ERROR", "message": message}
        finally:
            process_paths.remove(strpath)

    # We will again use pyout to provide a neat table summarizing our progress
    # with upload etc
    from .support import pyout as pyouts

    # for the upload speeds we need to provide a custom  aggregate
    t0 = time.time()

    def upload_agg(*ignored):
        dt = time.time() - t0
        # to help avoiding dict length changes during upload
        # might be not a proper solution
        # see https://github.com/dandi/dandi-cli/issues/502 for more info
        uploaded_recs = list(uploaded_paths.values())
        total = sum(v["size"] for v in uploaded_recs)
        if not total:
            return ""
        speed = total / dt if dt else 0
        return "%s/s" % naturalsize(speed)

    pyout_style = pyouts.get_style(hide_if_missing=False)
    pyout_style["upload"]["aggregate"] = upload_agg

    rec_fields = ["path", "size", "errors", "upload", "status", "message"]
    out = pyouts.LogSafeTabular(style=pyout_style, columns=rec_fields, max_workers=jobs)

    with out:
        for dfile in dandi_files:
            while len(process_paths) >= 10:
                lgr.log(2, "Sleep waiting for some paths to finish processing")
                time.sleep(0.5)

            process_paths.add(str(dfile.filepath))

            if isinstance(dfile, DandisetMetadataFile):
                rec = {"path": dandiset_metadata_file}
            else:
                assert isinstance(dfile, LocalAsset)
                rec = {"path": dfile.path}

            try:
                if devel_debug:
                    # DEBUG: do serially
                    for v in process_path(dfile):
                        print(str(v), flush=True)
                else:
                    rec[tuple(rec_fields[1:])] = process_path(dfile)
            except ValueError as exc:
                rec.update(skip_file(exc))
            out(rec)

    if sync:
        relpaths = []
        for p in original_paths:
            rp = os.path.relpath(p, dandiset.path)
            relpaths.append("" if rp == "." else rp)
        path_prefix = reduce(os.path.commonprefix, relpaths)
        to_delete = []
        for asset in remote_dandiset.get_assets_with_path_prefix(path_prefix):
            if (
                any(p == "" or path_is_subpath(asset.path, p) for p in relpaths)
                and not Path(dandiset.path, asset.path).exists()
            ):
                to_delete.append(asset)
        if to_delete and click.confirm(
            f"Delete {pluralize(len(to_delete), 'asset')} on server?"
        ):
            for asset in to_delete:
                asset.delete()
