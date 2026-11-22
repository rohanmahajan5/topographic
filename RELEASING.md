# Releasing Topographic

Each release is a Git tag `vX.Y.Z` and a GitHub release with two files whose names never change:
`Topographic-arm64.dmg` and `Topographic-arm64.zip`. Because the names stay the same,
`https://github.com/rohanmahajan5/topographic/releases/latest/download/Topographic-arm64.dmg` always gives the newest
build, and the app's update note links there.

## Steps (on an Apple silicon Mac with Node 22 and Python 3.11+)

1. Bump the version in `app/package.json` (and `app/package-lock.json`), for example to `0.2.0`, and commit:

   ```sh
   cd app
   npm version 0.2.0 --no-git-tag-version
   git commit -am "Topographic 0.2.0"
   ```

2. Build. The build only ships committed files, so commit first.

   ```sh
   npm ci
   npm test
   npm run build          # makes release/Topographic-arm64.dmg and release/Topographic-arm64.zip
   ```

3. Tag and publish the release (from the repository's top folder):

   ```sh
   git tag v0.2.0 && git push origin main v0.2.0
   gh release create v0.2.0 app/release/Topographic-arm64.dmg app/release/Topographic-arm64.zip \
     --title "Topographic 0.2.0" --notes "What changed, in a few lines." --latest
   ```

   `--latest` marks it as the latest release, which is what the app's update check and the README's Download button use.
   Never mark a test build as latest; use `--prerelease` for those (the app ignores pre-releases).

4. Check: open the README's Download link in a browser and confirm it downloads the new dmg; an older copy of the app shows
   "Topographic 0.2.0 is out" after **Topographic → Check for Updates…**.

While the repository is private, the update check gets a 404 from GitHub and stays silent; it starts working once the
repository is public.
