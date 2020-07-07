import json
import os
import posixpath
import shutil
import urllib.parse
import urllib.request
from functools import reduce

import requests
from picomc.downloader import DownloadQueue
from picomc.env import Env, file_verify_relative, get_filepath
from picomc.javainfo import get_java_info
from picomc.library import Library
from picomc.logging import logger
from picomc.rules import match_ruleset
from picomc.utils import die, file_sha1, recur_files


class VersionType:
    MAP = {"release": 1, "snapshot": 2, "old_alpha": 4, "old_beta": 8}

    def __init__(self, d=None):
        if isinstance(d, int):
            self._a = d
        elif isinstance(d, str):
            self._a = self.MAP[d]

    def __or__(self, other):
        return VersionType(self._a | other._a)

    def match(self, s):
        return bool(self.MAP[s] & self._a)

    @staticmethod
    def create(release, snapshot, alpha, beta):
        a = release | (2 * snapshot) | (4 * alpha) | (8 * beta)
        return VersionType(a)


VersionType.RELEASE = VersionType("release")
VersionType.SNAPSHOT = VersionType("snapshot")
VersionType.ALPHA = VersionType("old_alpha")
VersionType.BETA = VersionType("old_beta")

MODE_OVERRIDE = 0
MODE_REDUCE = 1


class CachedVspecAttr(object):
    def __init__(self, attr, mode=MODE_OVERRIDE, reduce_func=None, default=None):
        self.attr = attr
        self.mode = mode
        self.rfunc = reduce_func
        self.default = default

    def __get__(self, vspec, cls):
        if vspec is None:
            return self
        try:
            if self.mode == MODE_OVERRIDE:
                for v in vspec.chain:
                    if self.attr in v.raw_vspec:
                        r = v.raw_vspec[self.attr]
                        break
                else:
                    raise AttributeError()
            elif self.mode == MODE_REDUCE:
                try:
                    r = reduce(
                        self.rfunc,
                        (
                            v.raw_vspec[self.attr]
                            for v in vspec.chain[::-1]
                            if self.attr in v.raw_vspec
                        ),
                    )
                except TypeError as e:
                    raise AttributeError() from e
        except AttributeError:
            if self.default is not None:
                r = self.default(vspec.vobj)
        finally:
            try:
                setattr(vspec, self.attr, r)
                return r
            except UnboundLocalError as e:
                raise AttributeError() from e


def argumentadd(d1, d2):
    d = d1.copy()
    for k, v in d2.items():
        if k in d:
            d[k] += v
        else:
            d[k] = v
    return d


class VersionSpec:
    def __init__(self, vobj):
        self.vobj = vobj
        self.chain = self._resolve_chain()

    def _resolve_chain(self):
        chain = []
        chain.append(self.vobj)
        cv = self.vobj
        while "inheritsFrom" in cv.raw_vspec:
            cv = Env.vm.get_version(cv.raw_vspec["inheritsFrom"])
            chain.append(cv)
        return chain

    minecraftArguments = CachedVspecAttr("minecraftArguments")
    arguments = CachedVspecAttr("arguments", mode=MODE_REDUCE, reduce_func=argumentadd)
    mainClass = CachedVspecAttr("mainClass")
    assetIndex = CachedVspecAttr("assetIndex")
    libraries = CachedVspecAttr(
        "libraries", mode=MODE_REDUCE, reduce_func=lambda x, y: y + x
    )
    jar = CachedVspecAttr("jar", default=lambda vobj: vobj.version_name)
    downloads = CachedVspecAttr("downloads", default=lambda vobj: {})


class Version:
    ASSETS_URL = "http://resources.download.minecraft.net/"

    def __init__(self, version_name, version_manifest):
        self.version_name = version_name
        self.version_manifest = version_manifest
        self._libraries = dict()

        self.raw_vspec = self.get_raw_vspec()
        self.vspec = VersionSpec(self)

        self.raw_asset_index = self.get_raw_asset_index(self.vspec)

        jarname = self.vspec.jar
        self.jarfile = get_filepath("versions", jarname, "{}.jar".format(jarname))

    def get_raw_vspec(self):
        vspec_path = get_filepath(
            "versions", self.version_name, "{}.json".format(self.version_name)
        )
        if not self.version_manifest:
            if os.path.exists(vspec_path):
                logger.debug("Found custom vspec ({})".format(self.version_name))
                with open(vspec_path) as fp:
                    return json.load(fp)
            else:
                die("Specified version ({}) not available".format(self.version_name))
        url = self.version_manifest["url"]
        # Pull the hash out of the url. This is prone to breakage, maybe
        # just try to download the vspec and don't care about whether it
        # is up to date or not.
        url_split = urllib.parse.urlsplit(url)
        sha1 = posixpath.basename(posixpath.dirname(url_split.path))

        if os.path.exists(vspec_path) and file_sha1(vspec_path) == sha1:
            logger.debug(
                "Using cached vspec files, hash matches manifest ({})".format(
                    self.version_name
                )
            )
            with open(vspec_path) as fp:
                return json.load(fp)

        try:
            logger.debug("Downloading vspec file")
            raw = requests.get(url).content
            dirpath = os.path.dirname(vspec_path)
            os.makedirs(dirpath, exist_ok=True)
            with open(vspec_path, "wb") as fp:
                fp.write(raw)
            j = json.loads(raw)
            return j
        except requests.ConnectionError:
            die("Failed to retrieve version json file. Check your internet connection.")

    def get_raw_asset_index(self, vspec):
        iid = vspec.assetIndex["id"]
        url = vspec.assetIndex["url"]
        sha1 = vspec.assetIndex["sha1"]
        fpath = get_filepath("assets", "indexes", "{}.json".format(iid))
        if os.path.exists(fpath) and file_sha1(fpath) == sha1:
            logger.debug("Using cached asset index, hash matches vspec")
            with open(fpath) as fp:
                return json.load(fp)
        try:
            logger.debug("Downloading new asset index")
            raw = requests.get(url).content
            with open(fpath, "wb") as fp:
                fp.write(raw)
            return json.loads(raw)
        except requests.ConnectionError:
            die("Failed to retrieve asset index.")

    def get_libraries(self, java_info):
        if java_info is not None:
            key = java_info.get("java.home", None)
        else:
            key = None
        if key and key in self._libraries:
            return self._libraries[key]
        else:
            libs = []
            for lib in self.vspec.libraries:
                if "rules" in lib and not match_ruleset(lib["rules"], java_info):
                    continue
                lib_obj = Library(lib)
                if not lib_obj.available:
                    continue
                libs.append(lib_obj)
            if key:
                self._libraries[key] = libs
            return libs

    def get_jarfile_dl(self, verify_hashes=False, force=False):
        """Checks existence and hash of cached jar. Returns None if ok, otherwise
        returns download (url, size)"""
        logger.debug("Attempting to use jarfile: {}".format(self.jarfile))
        dlspec = self.vspec.downloads.get("client", None)
        if dlspec is None:
            logger.debug("jarfile dlspec not availble, skipping hash check.")
            if not os.path.exists(self.jarfile):
                die("jarfile does not exist and can not be downloaded.")
            return

        logger.debug("Checking jarfile.")
        if (
            force
            or not os.path.exists(self.jarfile)
            or (verify_hashes and file_sha1(self.jarfile) != dlspec["sha1"])
        ):
            logger.info(
                "Jar file ({}) will be downloaded with libraries.".format(
                    self.version_name
                )
            )
            return dlspec["url"], dlspec.get("size", None)

    def download_libraries(self, java_info, verify_hashes=False, force=False):
        """Downloads missing libraries."""
        logger.info("Checking libraries.")
        q = DownloadQueue()
        for library in self.get_libraries(java_info):
            if not library.available:
                continue
            basedir = get_filepath("libraries")
            abspath = library.get_abspath(basedir)
            ok = os.path.isfile(abspath) and os.path.getsize(abspath) > 0
            if verify_hashes and library.sha1 is not None:
                ok = ok and file_sha1(abspath) == library.sha1
            if not ok and not library.url:
                logger.error(
                    f"Library {library.libname} is missing or corrupt and has no download url."
                )
                continue
            if force or not ok:
                q.add(library.url, library.get_abspath(basedir), library.size)
        jardl = self.get_jarfile_dl(verify_hashes, force)
        if jardl is not None:
            url, size = jardl
            q.add(url, self.jarfile, size=size)
        if len(q) > 0:
            logger.info("Downloading {} libraries.".format(len(q)))
        if not q.download():
            logger.error(
                "Some libraries failed to download. If they are part of a non-vanilla profile, the original installer may need to be used."
            )

    def _populate_virtual_assets(self, where):
        for name, obj in self.raw_asset_index["objects"].items():
            sha = obj["hash"]
            objpath = get_filepath("assets", "objects", sha[0:2], sha)
            path = os.path.join(where, *name.split("/"))
            # Maybe check file hash first? Would that be faster?
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy(get_filepath(objpath), os.path.join(where, path))

    def get_virtual_asset_path(self):
        return get_filepath("assets", "virtual", self.vspec.assetIndex["id"])

    def prepare_assets_launch(self, gamedir):
        is_map_resources = self.raw_asset_index.get("map_to_resources", False)
        if is_map_resources:
            logger.info("Mapping resources")
            where = os.path.join(gamedir, "resources")
            logger.debug("Resources path: {}".format(where))
            self._populate_virtual_assets(where)

    def download_assets(self, verify_hashes=False, force=False):
        """Downloads missing assets."""

        hashes = dict()
        for obj in self.raw_asset_index["objects"].values():
            hashes[obj["hash"]] = obj["size"]

        logger.info("Checking {} assets.".format(len(hashes)))

        is_virtual = self.raw_asset_index.get("virtual", False)

        fileset = set(recur_files(get_filepath("assets")))
        q = DownloadQueue()
        objpath = get_filepath("assets", "objects")
        for sha in hashes:
            abspath = os.path.join(objpath, sha[0:2], sha)
            ok = abspath in fileset  # file exists
            if verify_hashes:
                ok = ok and file_sha1(abspath) == sha
            if force or not ok:
                url = urllib.parse.urljoin(
                    self.ASSETS_URL, posixpath.join(sha[0:2], sha)
                )
                q.add(url, abspath, size=hashes[sha])

        if len(q) > 0:
            logger.info("Downloading {} assets.".format(len(q)))
        if not q.download():
            logger.warn("Some assets failed to download.")

        if is_virtual:
            logger.info("Copying virtual assets")
            where = self.get_virtual_asset_path()
            logger.debug("Virtual asset path: {}".format(where))
            self._populate_virtual_assets(where)

    def prepare(self, java_info=None, verify_hashes=False):
        if not java_info:
            java_info = get_java_info(Env.gconf.get("java.path"))
        self.download_libraries(java_info, verify_hashes)
        self.download_assets(verify_hashes)

    def prepare_launch(self, gamedir, java_info, verify_hahes=False):
        self.prepare(java_info, verify_hahes)
        self.prepare_assets_launch(gamedir)


class VersionManager:
    MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"

    def __init__(self):
        self.manifest = self.get_manifest()

    def resolve_version_name(self, v):
        """Takes a metaversion and resolves to a version."""
        if v == "latest":
            v = self.manifest["latest"]["release"]
            logger.debug("Resolved latest -> {}".format(v))
        elif v == "snapshot":
            v = self.manifest["latest"]["snapshot"]
            logger.debug("Resolved snapshot -> {}".format(v))
        return v

    def get_manifest(self):
        manifest_filepath = get_filepath("versions", "manifest.json")
        try:
            m = requests.get(self.MANIFEST_URL).json()
            with open(manifest_filepath, "w") as mfile:
                json.dump(m, mfile, indent=4, sort_keys=True)
            return m
        except requests.ConnectionError:
            logger.warn(
                "Failed to retrieve version_manifest. "
                "Check your internet connection."
            )
            try:
                with open(manifest_filepath) as mfile:
                    logger.warn("Using cached version_manifest.")
                    return json.load(mfile)
            except FileNotFoundError:
                logger.warn("Cached version manifest not available.")
                raise RuntimeError("Failed to retrieve version manifest.")

    def version_list(self, vtype=VersionType.RELEASE, local=False):
        r = [v["id"] for v in self.manifest["versions"] if vtype.match(v["type"])]
        if local:
            import os

            r += sorted(
                "{} [local]".format(name)
                for name in os.listdir(get_filepath("versions"))
                if os.path.isdir(get_filepath("versions", name))
            )
        return r

    def get_version(self, version_name):
        version_manifest = None
        for ver in self.manifest["versions"]:
            if ver["id"] == version_name:
                version_manifest = ver
                break
        return Version(self.resolve_version_name(version_name), version_manifest)
