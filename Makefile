CURRENT_VERSION := $(shell cat VERSION | tr -d '[:space:]')
MAJOR           := $(word 1,$(subst ., ,$(CURRENT_VERSION)))
MINOR           := $(word 2,$(subst ., ,$(CURRENT_VERSION)))
PATCH           := $(word 3,$(subst ., ,$(CURRENT_VERSION)))

.PHONY: release-patch release-minor release-major

release-patch:
	$(eval NEXT := $(MAJOR).$(MINOR).$(shell echo $$(($(PATCH)+1))))
	$(call do-release,$(NEXT))

release-minor:
	$(eval NEXT := $(MAJOR).$(shell echo $$(($(MINOR)+1))).0)
	$(call do-release,$(NEXT))

release-major:
	$(eval NEXT := $(shell echo $$(($(MAJOR)+1))).0.0)
	$(call do-release,$(NEXT))

define do-release
	@echo "Bumping $(CURRENT_VERSION) -> $(1)"
	@printf '%s\n' "$(1)" > VERSION
	@git add VERSION
	@git commit -m "chore: release v$(1)"
	@git tag v$(1)
	@git push -u origin $$(git rev-parse --abbrev-ref HEAD)
	@git push origin v$(1)
	@echo "Released v$(1) — GitHub Actions will build and push the Docker image."
endef
