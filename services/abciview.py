"""
ABC iVIEW
Author: stabbedbybrick

Quality: up to 1080p

"""

import subprocess
import re
import base64

from urllib.parse import urlparse
from pathlib import Path
from collections import Counter

import click
import yaml

from bs4 import BeautifulSoup

from utils.utilities import (
    info,
    string_cleaning,
    print_info,
    set_save_path,
    set_filename,
    add_subtitles,
)
from utils.cdm import local_cdm, remote_cdm
from utils.titles import Episode, Series, Movie, Movies
from utils.args import Options, get_args
from utils.config import Config


class ABC(Config):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)

        if self.remote:
            info("Remote feature is not supported on this service")
            exit(1)

        if self.sub_only:
            info("Subtitle downloads are not supported on this service")
            exit(1)

        with open(Path("services") / "config" / "abciview.yaml", "r") as f:
            self.cfg = yaml.safe_load(f)

        self.config.update(self.cfg)

        self.lic_url = self.config["license"]
        self.get_options()

    def get_token(self):
        return self.client.post(
            self.config["jwt"],
            data={"clientId": self.config["client"]},
        ).json()["token"]

    def get_license(self, video_id: str):
        jwt = self.get_token()

        resp = self.client.get(
            self.config["drm"].format(video_id=video_id),
            headers={"bearer": jwt},
        ).json()

        if not resp["status"] == "ok":
            raise ValueError("Failed to fetch license token")

        return resp["license"]

    def get_data(self, url: str):
        show_id = urlparse(url).path.split("/")[2]
        url = self.config["series"].format(show=show_id)

        return self.client.get(url).json()

    def create_episode(self, episode):
        title = episode["showTitle"]
        season = re.search(r"Series (\d+)", episode.get("title"))
        number = re.search(r"Episode (\d+)", episode.get("title"))
        names_a = re.search(r"Series \d+ Episode \d+ (.+)", episode.get("title"))
        names_b = re.search(r"Series \d+ (.+)", episode.get("title"))

        name = (
            names_a.group(1)
            if names_a
            else names_b.group(1)
            if names_b
            else episode.get("displaySubtitle")
        )

        return Episode(
            id_=episode["id"],
            service="iV",
            title=title,
            season=int(season.group(1)) if season else 0,
            number=int(number.group(1)) if number else 0,
            name=name,
            description=episode.get("description"),
        )

    def get_series(self, url: str) -> Series:
        data = self.get_data(url)

        episodes = [
            self.create_episode(episode)
            for season in data
            for episode in reversed(season["_embedded"]["videoEpisodes"]["items"])
        ]
        return Series(episodes)

    def get_movies(self, url: str) -> Movies:
        slug = urlparse(url).path.split("/")[2]
        url = self.config["film"].format(slug=slug)

        data = self.client.get(url).json()

        return Movies(
            [
                Movie(
                    id_=data["_embedded"]["highlightVideo"]["id"],
                    service="iV",
                    title=data["title"],
                    name=data["title"],
                    year=data.get("productionYear"),
                    synopsis=data.get("description"),
                )
            ]
        )

    def get_pssh(self, soup: str) -> str:
        try:
            kid = (
                soup.select_one("ContentProtection")
                .attrs.get("cenc:default_KID")
                .replace("-", "")
            )
        except:
            raise AttributeError("Video unavailable outside of Australia")

        array_of_bytes = bytearray(b"\x00\x00\x002pssh\x00\x00\x00\x00")
        array_of_bytes.extend(bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed"))
        array_of_bytes.extend(b"\x00\x00\x00\x12\x12\x10")
        array_of_bytes.extend(bytes.fromhex(kid.replace("-", "")))
        return base64.b64encode(bytes.fromhex(array_of_bytes.hex())).decode("utf-8")

    def get_mediainfo(self, manifest: str, quality: str, subtitle: str) -> str:
        self.soup = BeautifulSoup(self.client.get(manifest), "xml")
        pssh = self.get_pssh(self.soup)
        elements = self.soup.find_all("Representation")
        heights = sorted(
            [int(x.attrs["height"]) for x in elements if x.attrs.get("height")],
            reverse=True,
        )

        _base = re.sub(r"(\d+.mpd)", "", manifest)

        base_urls = self.soup.find_all("BaseURL")
        for base in base_urls:
            base.string = _base + base.string

        if quality is not None:
            if int(quality) in heights:
                return quality, pssh
            else:
                closest_match = min(heights, key=lambda x: abs(int(x) - int(quality)))
                info(f"Resolution not available. Getting closest match:")
                return closest_match, pssh

        if subtitle is not None:
            self.soup = add_subtitles(self.soup, subtitle)

        with open(self.tmp / "manifest.mpd", "w") as f:
            f.write(str(self.soup.prettify()))

        return heights[0], pssh

    def get_playlist(self, video_id: str) -> tuple:
        resp = self.client.get(self.config["vod"].format(video_id=video_id)).json()

        try:
            playlist = resp["_embedded"]["playlist"]
        except:
            raise KeyError(resp["unavailableMessage"])
        
        streams = [
            x["streams"]["mpegdash"] 
            for x in playlist 
            if x["type"] == "program"
        ][0]

        if streams.get("720"):
            manifest = streams["720"].replace("720.mpd", "1080.mpd")
        else:
            manifest = streams["sd"]

        subtitle = [
            x["captions"].get("src-vtt") 
            for x in playlist 
            if x["type"] == "program"
        ][0]

        return manifest, subtitle

    def get_content(self, url: str) -> object:
        if self.movie:
            with self.console.status("Fetching titles..."):
                content = self.get_movies(self.url)
                title = string_cleaning(str(content))

            info(f"{str(content)}\n")

        else:
            with self.console.status("Fetching titles..."):
                content = self.get_series(url)

                title = string_cleaning(str(content))
                seasons = Counter(x.season for x in content)
                num_seasons = len(seasons)
                num_episodes = sum(seasons.values())

            info(
                f"{str(content)}: {num_seasons} Season(s), {num_episodes} Episode(s)\n"
            )

        return content, title

    def get_episode_from_url(self, url: str):
        video_id = urlparse(url).path.split("/")[2]

        data = self.client.get(self.config["vod"].format(video_id=video_id)).json()

        episode = self.create_episode(data)

        episode = Series([episode])

        title = string_cleaning(str(episode))

        return [episode[0]], title

    def get_options(self) -> None:
        opt = Options(self)

        if self.url and not any(
            [self.episode, self.season, self.complete, self.movie, self.titles]
        ):
            downloads, title = self.get_episode_from_url(self.url)

        else:
            content, title = self.get_content(self.url)

            if self.episode:
                downloads = opt.get_episode(content)
            if self.season:
                downloads = opt.get_season(content)
            if self.complete:
                downloads = opt.get_complete(content)
            if self.movie:
                downloads = opt.get_movie(content)
            if self.titles:
                opt.list_titles(content)

        for download in downloads:
            self.download(download, title)

    def download(self, stream: object, title: str) -> None:
        with self.console.status("Getting media info..."):
            manifest, subtitle = self.get_playlist(stream.id)
            res, pssh = self.get_mediainfo(manifest, self.quality, subtitle)
            customdata = self.get_license(stream.id)
            self.client.headers.update({"customdata": customdata})

        with self.console.status("Getting decryption keys..."):
            keys = local_cdm(pssh, self.lic_url, self.client)

            with open(self.tmp / "keys.txt", "w") as file:
                file.write("\n".join(keys))

        if self.info:
            print_info(self, stream, keys)

        self.filename = set_filename(self, stream, res, audio="AAC2.0")
        self.save_path = set_save_path(stream, self.config, title)
        self.manifest = self.tmp / "manifest.mpd"
        self.key_file = self.tmp / "keys.txt"
        self.sub_path = None

        info(f"{str(stream)}")
        info(f"{keys[0]}")
        click.echo("")

        args, file_path = get_args(self, res)

        if not file_path.exists():
            try:
                subprocess.run(args, check=True)
            except:
                raise ValueError("Download failed or was interrupted")
        else:
            info(f"{self.filename} already exist. Skipping download\n")
            self.sub_path.unlink() if self.sub_path else None
            pass
