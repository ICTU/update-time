# Warn once about a Maven artefact whose version another scanned pom manages

## What Update-time does today

Update-time checks each `<dependency>` element a pom declares on its own. Since #379, that includes a dependency that leaves its `<version>` out, because a `<dependencyManagement>` section manages it. Update-time checks such a dependency for staleness, archival, and vulnerabilities, at the version Maven's effective pom gives it.

So an artefact that a pom manages, and also declares without a `<version>`, gets every warning once per declaration. For this pom:

```xml
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>junit</groupId>
        <artifactId>junit</artifactId>
        <version>4.13.2</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
    </dependency>
  </dependencies>
```

Update-time warns twice:

```console
WARNING  Stale dependency junit:junit in pom.xml:11: newest release 4.13.2 was published 2053 days ago (> 365)
WARNING  Stale dependency junit:junit in pom.xml:16: newest release 4.13.2 was published 2053 days ago (> 365)
```

A multi-module project repeats the warning further. The parent pom usually manages the version, and each module declares the artefact without a `<version>`. The Maven run over the parent pom warns about the managed declaration. The Maven run over each module warns again, about that module's declaration. With ten modules, one stale artefact gets eleven warnings. A vulnerability or an archival warning repeats the same way.

The warnings about the versionless declarations say nothing new when the scan also covers the pom that manages the version. The run over that pom already checks the managed declaration, at the same version. A versionless declaration adds a check only where the managing pom sits outside the scan: a released parent pom on Maven Central, or a BOM the project imports.

The README's Maven section documents the current behaviour.

## What users see

Update-time warns once about an artefact whose version a scanned pom manages, at the managed declaration. It skips the staleness, archival, and vulnerability checks for each declaration that leaves its `<version>` to that pom. Update-time checks a declaration as it does today where a pom outside the scan manages its version.

## Proposal

The effective pom's `<version>` element carries an input location naming the pom that manages the version, such as `<!-- org.example:parent:1.0, line 11 -->`. Maven 3.9.16 with the help plugin 3.5.2 writes it for a version managed by the scanned pom itself, too. Update-time skips a versionless declaration whose input location names a pom the scan covers, the scanned pom itself included.

This needs three changes:

1. **The effective pom on the staleness and archival path.** The OSV check reads the effective pom already. The staleness and archival checks read the pom alone, through `pom_xml.artefact_references`. They run after Maven, so the effective pom is available to them.
2. **The coordinates of every scanned pom.** An input location names a pom by `groupId:artifactId:version`. Update-time would read those coordinates for each pom the glob finds, before the first Maven run. A pom that leaves out its group or version inherits it from its `<parent>` element. Reading the coordinates does not cost a request.
3. **A fallback for coordinates Update-time cannot resolve.** A pom can spell its version as a property, such as `${revision}`. Update-time then cannot tell whether an input location names that pom, so it checks the versionless declaration as it does today. A duplicate warning is better than a missing one.

## Open questions

- **Markers (#382).** A marker on a versionless declaration steers checks that this issue skips. Update-time could report such a marker as redundant. Or the managed declaration's marker could steer both declarations. Which should it do?

## Scope — excluded

- **Plugins.** A `<plugin>` without a `<version>` repeats its warnings the same way, when a `<pluginManagement>` section manages it. The effective pom marks plugin versions too, but `pom_xml._effective_versions` reads dependencies alone. Plugins can follow once this lands.
- **A pom outside the scan.** A released parent pom or an imported BOM keeps today's behaviour: each versionless declaration it manages is checked.

## Documentation

The Maven section "What dependencies are updated?" in the README says that each declaration is checked on its own. It changes to say that Update-time warns once about an artefact a scanned pom manages. The changelog gets an entry naming the behaviour.
