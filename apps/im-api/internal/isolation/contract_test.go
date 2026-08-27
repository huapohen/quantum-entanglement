package isolation

import (
	"errors"
	"reflect"
	"testing"
	"time"
)

var executionContractTime = time.Date(2026, 8, 28, 12, 0, 0, 0, time.FixedZone("CST", 8*60*60))

func TestIsolationProfileRejectsAmbientHostAuthority(t *testing.T) {
	t.Parallel()

	base := validIsolationProfile(t)
	testCases := []struct {
		name   string
		mutate func(*ExecutionIsolationProfile)
	}{
		{"writable root", func(profile *ExecutionIsolationProfile) { profile.Filesystem.ReadOnlyRoot = false }},
		{"host home", func(profile *ExecutionIsolationProfile) { profile.Filesystem.MountHostHome = true }},
		{"runtime socket", func(profile *ExecutionIsolationProfile) { profile.Filesystem.MountRuntimeSocket = true }},
		{"shared uid", func(profile *ExecutionIsolationProfile) { profile.Process.SeparateUID = false }},
		{"privileged", func(profile *ExecutionIsolationProfile) { profile.Process.Privileged = true }},
		{"host pid", func(profile *ExecutionIsolationProfile) { profile.Process.HostPID = true }},
		{"host network", func(profile *ExecutionIsolationProfile) { profile.Network.HostNetwork = true }},
		{"unbounded process", func(profile *ExecutionIsolationProfile) { profile.Process.MaxProcesses = 0 }},
		{"unbounded memory", func(profile *ExecutionIsolationProfile) { profile.Resources.MemoryBytes = 0 }},
		{"unbounded wall time", func(profile *ExecutionIsolationProfile) { profile.Resources.WallTime = 0 }},
	}
	for _, testCase := range testCases {
		testCase := testCase
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			candidate := base
			testCase.mutate(&candidate)
			if _, err := SealExecutionIsolationProfile(candidate); !errors.Is(err, ErrInvalidIsolationProfile) {
				t.Fatalf("seal error = %v, want %v", err, ErrInvalidIsolationProfile)
			}
		})
	}
}

func TestIsolationProfileDigestDetectsPolicyDrift(t *testing.T) {
	t.Parallel()

	profile := validIsolationProfile(t)
	if err := ValidateExecutionIsolationProfile(profile); err != nil {
		t.Fatalf("validate profile: %v", err)
	}
	drifted := profile
	drifted.Resources.MemoryBytes++
	if err := ValidateExecutionIsolationProfile(drifted); !errors.Is(err, ErrInvalidIsolationProfile) {
		t.Fatalf("drift error = %v, want %v", err, ErrInvalidIsolationProfile)
	}
}

func TestRuntimeGrantBindsEveryExecutionScopeAndReturnsSnapshots(t *testing.T) {
	t.Parallel()

	profile := validIsolationProfile(t)
	packageVersion := validPackageVersion()
	grant := validRuntimeGrant(t, packageVersion, profile)
	if err := ValidateRuntimeGrantAt(grant, executionContractTime); err != nil {
		t.Fatalf("validate grant: %v", err)
	}

	mutations := []struct {
		name   string
		mutate func(*RuntimeGrant)
	}{
		{"tenant", func(value *RuntimeGrant) { value.TenantID = "tenant-other" }},
		{"workspace", func(value *RuntimeGrant) { value.WorkspaceID = "workspace-other" }},
		{"task", func(value *RuntimeGrant) { value.TaskID = "task-other" }},
		{"attempt", func(value *RuntimeGrant) { value.AttemptID = "attempt-other" }},
		{"action", func(value *RuntimeGrant) { value.ActionID = "action-other" }},
		{"execution", func(value *RuntimeGrant) { value.ExecutionID = "execution-other" }},
		{"package", func(value *RuntimeGrant) { value.PackageArtifactDigest = digestOf('b') }},
		{"manifest", func(value *RuntimeGrant) { value.PackageManifestDigest = digestOf('c') }},
		{"admission", func(value *RuntimeGrant) { value.PackageAdmissionRevision++ }},
		{"profile", func(value *RuntimeGrant) { value.IsolationProfileDigest = digestOf('c') }},
		{"previous generation", func(value *RuntimeGrant) { value.ExpectedPreviousGeneration++ }},
		{"policy", func(value *RuntimeGrant) { value.PolicyRevision++ }},
		{"approval", func(value *RuntimeGrant) { value.ApprovalRevision++ }},
		{"revocation", func(value *RuntimeGrant) { value.RevocationEpoch++ }},
		{"issued", func(value *RuntimeGrant) { value.IssuedAt = value.IssuedAt.Add(-time.Second) }},
		{"deadline", func(value *RuntimeGrant) { value.ExpiresAt = value.ExpiresAt.Add(time.Second) }},
		{"effect class", func(value *RuntimeGrant) {
			value.EffectClass = EffectPure
			value.CapabilityBindings = nil
			value.SecretBindings = nil
			value.EgressBindings = nil
		}},
		{"capability", func(value *RuntimeGrant) { value.CapabilityBindings = []SHA256Digest{digestOf('e')} }},
		{"secret", func(value *RuntimeGrant) { value.SecretBindings = []SHA256Digest{digestOf('f')} }},
		{"egress", func(value *RuntimeGrant) { value.EgressBindings = []SHA256Digest{digestOf('1')} }},
	}
	for _, mutation := range mutations {
		candidate := grant
		candidate.CapabilityBindings = append([]SHA256Digest(nil), grant.CapabilityBindings...)
		candidate.SecretBindings = append([]SHA256Digest(nil), grant.SecretBindings...)
		candidate.EgressBindings = append([]SHA256Digest(nil), grant.EgressBindings...)
		mutation.mutate(&candidate)
		sealed, err := SealRuntimeGrant(candidate)
		if err != nil {
			t.Fatalf("seal %s: %v", mutation.name, err)
		}
		if sealed.Digest == grant.Digest {
			t.Fatalf("%s drift did not change grant digest", mutation.name)
		}
	}

	input := []SHA256Digest{digestOf('3'), digestOf('2')}
	grant.CapabilityBindings = input
	sealed, err := SealRuntimeGrant(grant)
	if err != nil {
		t.Fatalf("seal sorted grant: %v", err)
	}
	input[0] = digestOf('4')
	if got := sealed.CapabilityBindings[1]; got != digestOf('3') {
		t.Fatalf("grant alias mutated snapshot: %s", got)
	}
}

func TestLaunchFailsClosedOnExpiredOrCrossBoundGrant(t *testing.T) {
	t.Parallel()

	profile := validIsolationProfile(t)
	packageVersion := validPackageVersion()
	grant := validRuntimeGrant(t, packageVersion, profile)
	request := ResolvedLaunchAdmission{Package: packageVersion, Profile: profile, Grant: grant}
	if err := ValidateResolvedLaunchAdmissionAt(request, executionContractTime); err != nil {
		t.Fatalf("validate launch: %v", err)
	}
	if err := ValidateResolvedLaunchAdmissionAt(request, grant.ExpiresAt); !errors.Is(err, ErrInvalidLaunchRequest) {
		t.Fatalf("expired error = %v, want %v", err, ErrInvalidLaunchRequest)
	}

	wrongPackage := request
	wrongPackage.Package.ArtifactDigest = digestOf('9')
	if err := ValidateResolvedLaunchAdmissionAt(wrongPackage, executionContractTime); !errors.Is(err, ErrInvalidLaunchRequest) {
		t.Fatalf("package mismatch error = %v, want %v", err, ErrInvalidLaunchRequest)
	}
	wrongProfile := request
	wrongProfile.Profile = validIsolationProfileWithID(t, "profile-other")
	if err := ValidateResolvedLaunchAdmissionAt(wrongProfile, executionContractTime); !errors.Is(err, ErrInvalidLaunchRequest) {
		t.Fatalf("profile mismatch error = %v, want %v", err, ErrInvalidLaunchRequest)
	}
}

func TestPureRuntimeGrantCannotCarryEgressAuthority(t *testing.T) {
	t.Parallel()

	profile := validIsolationProfile(t)
	packageVersion := validPackageVersion()
	grant := validRuntimeGrant(t, packageVersion, profile)
	grant.EffectClass = EffectPure
	if _, err := SealRuntimeGrant(grant); !errors.Is(err, ErrInvalidRuntimeGrant) {
		t.Fatalf("pure grant with egress error = %v, want %v", err, ErrInvalidRuntimeGrant)
	}
	grant.CapabilityBindings = nil
	grant.SecretBindings = nil
	grant.EgressBindings = nil
	if _, err := SealRuntimeGrant(grant); err != nil {
		t.Fatalf("seal pure grant without egress: %v", err)
	}
}

func TestProcessFenceRejectsOldGenerationAndCrossTenantReplay(t *testing.T) {
	t.Parallel()

	instance := exampleProcessInstance()
	if err := ValidateProcessFence(instance, instance.Fence()); err != nil {
		t.Fatalf("validate exact fence: %v", err)
	}
	stale := instance.Fence()
	stale.Generation--
	if err := ValidateProcessFence(instance, stale); !errors.Is(err, ErrStaleGeneration) {
		t.Fatalf("stale error = %v, want %v", err, ErrStaleGeneration)
	}
	crossTenant := instance.Fence()
	crossTenant.TenantID = "tenant-other"
	if err := ValidateProcessFence(instance, crossTenant); !errors.Is(err, ErrStaleGeneration) {
		t.Fatalf("cross-tenant error = %v, want %v", err, ErrStaleGeneration)
	}
}

func TestLaunchContractContainsNoInProcessCallbackOrPrivilegedHandle(t *testing.T) {
	t.Parallel()

	assertDataOnlyType(t, reflect.TypeOf(ResolvedLaunchAdmission{}), map[reflect.Type]bool{})
	assertDataOnlyType(t, reflect.TypeOf(ProcessInstance{}), map[reflect.Type]bool{})
}

func assertDataOnlyType(t *testing.T, value reflect.Type, visited map[reflect.Type]bool) {
	t.Helper()
	if visited[value] {
		return
	}
	visited[value] = true
	switch value.Kind() {
	case reflect.Func, reflect.Interface, reflect.Chan, reflect.UnsafePointer, reflect.Pointer:
		t.Fatalf("%s contains runtime authority kind %s", value, value.Kind())
	case reflect.Struct:
		for index := 0; index < value.NumField(); index++ {
			field := value.Field(index)
			if field.PkgPath == "" {
				assertDataOnlyType(t, field.Type, visited)
			}
		}
	case reflect.Slice, reflect.Array:
		assertDataOnlyType(t, value.Elem(), visited)
	}
}

func validPackageVersion() ExecutablePackageVersion {
	return ExecutablePackageVersion{
		SchemaVersion:     1,
		PackageID:         "package.agent.v1",
		Version:           "1.0.0",
		ArtifactDigest:    digestOf('a'),
		ManifestDigest:    digestOf('b'),
		AdmissionRevision: 7,
	}
}

func validIsolationProfile(t *testing.T) ExecutionIsolationProfile {
	t.Helper()
	return validIsolationProfileWithID(t, "profile.default-deny.v1")
}

func validIsolationProfileWithID(t *testing.T, profileID string) ExecutionIsolationProfile {
	t.Helper()
	profile, err := SealExecutionIsolationProfile(ExecutionIsolationProfile{
		SchemaVersion: 1,
		ProfileID:     profileID,
		Revision:      3,
		Kind:          IsolationContainer,
		Filesystem:    FilesystemPolicy{ReadOnlyRoot: true, Workspace: WorkspaceEphemeralRW},
		Process:       ProcessPolicy{SeparateUID: true, MaxProcesses: 32},
		Network:       NetworkPolicy{Mode: NetworkBrokerOnly},
		Resources: ResourcePolicy{
			MemoryBytes: 512 << 20,
			DiskBytes:   2 << 30,
			CPUTime:     2 * time.Minute,
			WallTime:    5 * time.Minute,
		},
	})
	if err != nil {
		t.Fatalf("seal profile: %v", err)
	}
	return profile
}

func validRuntimeGrant(
	t *testing.T,
	packageVersion ExecutablePackageVersion,
	profile ExecutionIsolationProfile,
) RuntimeGrant {
	t.Helper()
	grant, err := SealRuntimeGrant(RuntimeGrant{
		SchemaVersion:              1,
		GrantID:                    "grant-1",
		TenantID:                   "tenant-acme",
		WorkspaceID:                "workspace-acme",
		TaskID:                     "task-1",
		AttemptID:                  "attempt-1",
		ActionID:                   "action-1",
		ExecutionID:                "execution-1",
		PackageArtifactDigest:      packageVersion.ArtifactDigest,
		PackageManifestDigest:      packageVersion.ManifestDigest,
		PackageAdmissionRevision:   packageVersion.AdmissionRevision,
		IsolationProfileDigest:     profile.Digest,
		ExpectedPreviousGeneration: 3,
		PolicyRevision:             11,
		ApprovalRevision:           13,
		RevocationEpoch:            17,
		IssuedAt:                   executionContractTime.Add(-time.Minute),
		ExpiresAt:                  executionContractTime.Add(time.Hour),
		MaxUses:                    1,
		EffectClass:                EffectExternal,
		CapabilityBindings:         []SHA256Digest{digestOf('5')},
		SecretBindings:             []SHA256Digest{digestOf('6')},
		EgressBindings:             []SHA256Digest{digestOf('7')},
	})
	if err != nil {
		t.Fatalf("seal grant: %v", err)
	}
	return grant
}

func exampleProcessInstance() ProcessInstance {
	return ProcessInstance{
		InstanceID:             "instance-1",
		ExecutionID:            "execution-1",
		GrantID:                "grant-1",
		TenantID:               "tenant-acme",
		TaskID:                 "task-1",
		AttemptID:              "attempt-1",
		PackageArtifactDigest:  digestOf('a'),
		PackageManifestDigest:  digestOf('b'),
		IsolationProfileDigest: digestOf('b'),
		RuntimeGrantDigest:     digestOf('c'),
		Generation:             4,
		FenceRevision:          2,
		FenceDigest:            digestOf('4'),
		State:                  ProcessRunning,
	}
}

func digestOf(value byte) SHA256Digest {
	return SHA256Digest("sha256:" + string(makeFilledBytes(value, 64)))
}

func makeFilledBytes(value byte, count int) []byte {
	output := make([]byte, count)
	for index := range output {
		output[index] = value
	}
	return output
}
