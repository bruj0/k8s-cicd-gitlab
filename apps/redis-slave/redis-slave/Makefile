VERSION?=v3
REGISTRY?=localhost:5000

release: clean build push clean

# builds a docker image that builds the app and packages it into a minimal docker image
build:
	docker buildx build --load -t gb-redisslave:${VERSION} .

# push the image to an registry
push:
	docker buildx build --push -t ${REGISTRY}/gb-redisslave:${VERSION} .

load:
	kind load docker-image gb-redisslave:${VERSION} gb-redisslave:${VERSION} --name kind
# remove previous images and containers
clean:
	docker rm -f ${REGISTRY}/guestbook:${VERSION} 2> /dev/null || true

.PHONY: release clean build push