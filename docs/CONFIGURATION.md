# Configuration

Rosé is configured by a TOML file.

The configuration, by default, is located at `${XDG_CONFIG_HOME:-$HOME/.config}/rose/config.toml`
on Linux and `~/Library/Preferences/rose/config.toml` on MacOS. The
`--config/-c` flag can be specified to load a configuration file from a
different location.

The configuration parameters, with examples, are:

```toml
# =======================
# === Required values ===
# =======================

# The directory containing the music to manage. Rosé has strong expectations
# towards the organization of this directory.
#
# All releases must be immediate child directories of the music_source_dir. And
# Rosé expects that all tracks belong to a "release" (meaning an album, single,
# EP, etc.). Therefore, loose audio files at the top-level of the source
# directory will be ignored.
#
# Rosé also writes collages and playlists to this directory, as `!collages` and
# `!playlists` subdirectories.
music_source_dir = "~/.music-source"

# The directory to mount the virtual filesystem on.
fuse_mount_dir = "~/music"

# =======================
# === Optional values ===
# =======================

# Artist aliases: Grouping multiple names for the same artist together.
#
# Artists will sometimes release under multiple names. This is fine, but
# wouldn't it be nice if all releases by an artist, regardless of whichever
# alias was used, appeared in a single spot?
#
# That's what this configuration option enables. This configuration option
# makes the releases of "aliased" artists also appear under the main artist in
# the Artists browsing view.
artist_aliases = [
  { artist = "Abakus", aliases = ["Cinnamon Chasers"] },
  { artist = "tripleS", aliases = ["EVOLution", "LOVElution", "+(KR)ystal Eyes", "Acid Angel From Asia", "Acid Eyes"] },
]

# Artists, genres, and labels to show in their respective top-level virtual
# filesystem directories. By # default, all artists, genres, and labels are
# shown. However, if this configuration parameter is specified, the list can be
# restricted to a specific few values. This is useful if you only care about a
# few specific genres and labels.
fuse_artists_whitelist = [ "xxx", "yyy" ]
fuse_genres_whitelist = [ "xxx", "yyy" ]
fuse_labels_whitelist = [ "xxx", "yyy" ]
# Artists, genres, and labels to hide from the virtual filesystem navigation.
# These options remove specific entities from their respective top-level
# virtual filesystem directories. This is useful if there are a few values you
# don't find useful, e.g. a random featuring artist or one super niche genre.
#
# These options are mutually exclusive with the fuse_*_whitelist options; if
# both are specified for a given entity type, the configuration will not
# validate.
fuse_artists_blacklist = [ "xxx" ]
fuse_genres_blacklist = [ "xxx" ]
fuse_labels_blacklist = [ "xxx" ]

# When Rosé scans a release directory, it looks for cover art that matches:
#
# 1. A supported file "stem" (the filename excluding the extension).
# 2. A supported file "extension" (the file type basically).
#
# And when Rosé scans the playlists directory, it looks for art files that
# match a supported file extension (but not the name, as the name should match
# the playlist name).
#
# By default, Rosé matches the stems "folder", "cover", "art", and "front"; and
# the extensions "jpg", "jpeg", and "png". Comparisons are case insensitive,
# meaning Rosé will also match FOLDER.PNG.
#
# If you wish to recognize additional file stems and/or extensions, you can set
# the below two variables.
cover_art_stems = [ "folder", "cover", "art", "front" ]
valid_art_exts = [ "jpg", "jpeg", "png" ]

# You may have some directories in your music source directory that should not
# be treated like releases. You can make Rosé ignore them by adding the
# directory names to this configuration variable. For example, if you use
# Syncthing versioning, the `.stversions` directory will contain music files,
# but Rosé should not scan them.
#
# By default, `!collages` and # `!playlists` are ignored. You do not need to
# add them to your ignore list: they will be ignored regardless of this
# configuration variable.
ignore_release_directories = [ ".stversions" ]

# The directory to write the cache to. Defaults to:
# - Linux: `${XDG_CACHE_HOME:-$HOME/.cache}/rose`
# - MacOS: `~/Library/Caches/rose`
cache_dir = "~/.cache/rose"

# Maximum parallel processes that Rose can spawn. Defaults to # $(nproc)/2.
#
# Rose uses this value to limit the max parallelization of read cache updates
# and the number of works that the virtual filesystem can spin up to handle a
# request.
max_proc = 4
```

# Shell Completion

Rosé supports optional shell completion for the `bash`, `zsh`, and `fish`
shells. The following commands enable shell completion:

```bash
# Bash
$ rose completion bash > ~/.config/rose/completion.bash
$ echo ". ~/.config/rose/.completion.bash" >> ~/.bashrc

# Zsh
$ rose completion zsh > ~/.config/rose/completion.zsh
$ echo ". ~/.config/rose/.completion.zsh" >> ~/.zshrc

# Fish
$ rose completion fish > ~/.config/fish/completions/rose.fish
```

# Systemd

By default, the `rose fs mount` and `rose cache watch` commands spawn daemons
that run in the background.

However, these daemons do not recover from failure or start on boot. If you
would like them to, you can manage Rosé's processes with a service manager such
as systemd.

Some sample systemd service files for managing Rosé are:

```ini
TODO
```
